"""Application FastAPI.

L'interface lit la base ; les seuls appels sortants sont l'aperçu live, le
résolveur de revue et le bouton « Rafraîchir » — tous déclenchés
explicitement par l'utilisateur. Toutes les réponses sont des fragments HTML
consommés par HTMX.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote_plus

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import citation, relevance, repo
from app.config import PROJECT_ROOT, settings
from app.db import connect, init_db, relevance_enabled
from app.llm import OllamaClient
from app.pubmed import PubMedClient
from app.refresh import RefreshReport, refresh_query
from app.scheduler import create_scheduler

# uvicorn ne configure que ses propres loggers : sans cela, les messages du job
# de fond n'apparaîtraient jamais dans la console.
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(name)s : %(message)s"
)

TEMPLATES_DIR = PROJECT_ROOT / "app" / "templates"
STATIC_DIR = PROJECT_ROOT / "app" / "static"
PUBMED_SEARCH_URL = "https://pubmed.ncbi.nlm.nih.gov/?term="


def date_fr(value: str | None) -> str:
    """Met une date ISO au format français, en tolérant les dates partielles.

    PubMed renvoie parfois « 2026 » ou « 2026-03 » seuls : on affiche ce qu'on
    a plutôt que d'inventer un jour.
    """
    if not value:
        return "date inconnue"
    date_part, _, time_part = value.partition("T")
    pieces = date_part.split("-")
    if len(pieces) == 3:
        rendered = f"{pieces[2]}/{pieces[1]}/{pieces[0]}"
    elif len(pieces) == 2:
        rendered = f"{pieces[1]}/{pieces[0]}"
    else:
        return date_part
    if time_part:
        rendered += f" à {time_part[:5]}"
    return rendered


def date_courte(value: str | None) -> str:
    """Jour et mois seuls — pour les étiquettes étroites de la barre latérale."""
    if not value:
        return "jamais"
    pieces = value.partition("T")[0].split("-")
    return f"{pieces[2]}/{pieces[1]}" if len(pieces) == 3 else pieces[0]


@asynccontextmanager
async def lifespan(app_: FastAPI):
    # `state.db_path` détourne l'initialisation vers une base jetable : sans
    # lui, monter l'application dans un test migre la base de production.
    init_db(getattr(app_.state, "db_path", None))
    scheduler = create_scheduler()
    if scheduler is not None:
        scheduler.start()
    try:
        yield
    finally:
        if scheduler is not None:
            # wait=False : on n'attend pas la fin d'un rafraîchissement en cours,
            # il reprendra au prochain démarrage.
            scheduler.shutdown(wait=False)


app = FastAPI(title="AutoPubMed", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.filters["date_fr"] = date_fr
templates.env.filters["date_courte"] = date_courte
# Le bouton de résumé n'apparaît que si la fonction est activée. On ne sonde
# pas Ollama ici : un service démarré après l'application doit rester
# utilisable sans redémarrer, et un service arrêté donne un message clair.
templates.env.globals["llm_summary_enabled"] = settings.enable_llm_summary
# Fonction et non valeur : les gabarits lisent le drapeau à chaque rendu, comme
# le dépôt, et les tests peuvent le basculer.
templates.env.globals["relevance_enabled"] = relevance_enabled
templates.env.globals["rating_labels"] = repo.RATING_LABELS


def get_db() -> Iterator[sqlite3.Connection]:
    """Une connexion par requête HTTP, fermée à la fin de celle-ci."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _optional_number[T: (int, float)](raw: str | None, cast: type[T]) -> T | None:
    """Un champ de formulaire vide vaut « pas de filtre », pas zéro."""
    raw = (raw or "").strip().replace(",", ".")
    try:
        return cast(raw) if raw else None
    except ValueError as exc:
        raise HTTPException(422, detail=f"Nombre attendu, reçu : {raw!r}") from exc


def _sidebar_context(conn: sqlite3.Connection) -> dict[str, Any]:
    """Ce dont `_sidebar.html` a besoin, où qu'il soit inclus."""
    return {
        "folders": repo.folders_with_queries(conn),
        "favorites_count": repo.count_favorites(conn),
        "new_count": repo.count_new_articles(conn),
    }


def _learning_context(conn: sqlite3.Connection, query_id: int) -> dict[str, Any]:
    """Jauge d'apprentissage et seuil de masquage. None si la couche est éteinte."""
    if not relevance_enabled():
        return {"learning": None, "hide_below": None}
    return {
        "learning": relevance.learning_status(repo.ratings_for_query(conn, query_id)),
        "hide_below": repo.get_hide_below(conn, query_id),
    }


def _list_params(favorites_only: bool, show_hidden: bool) -> str:
    """Paramètres d'URL qui définissent la liste affichée, pour la page suivante."""
    return ("favorites=1&" if favorites_only else "") + (
        "show_hidden=1&" if show_hidden else ""
    )


def _list_context(
    conn: sqlite3.Connection,
    query: Any,
    *,
    favorites_only: bool,
    show_hidden: bool,
    offset: int,
) -> dict[str, Any]:
    """Contexte d'une liste de requête : articles, compteurs, pagination.

    Partagé par l'ouverture, le rafraîchissement et « tout marquer comme lu »,
    pour que le masquage s'applique partout de la même façon.
    """
    return {
        "query": query,
        "articles": repo.list_articles_for_query(
            conn,
            query.id,
            query.sort,
            max_age_years=query.max_age_years,
            favorites_only=favorites_only,
            limit=repo.PAGE_SIZE,
            offset=offset,
            show_hidden=show_hidden,
        ),
        "favorites_only": favorites_only,
        "show_hidden": show_hidden,
        "unread": repo.count_unread(conn, query.id, show_hidden),
        "hidden_count": repo.count_hidden_for_query(
            conn, query.id, query.max_age_years, favorites_only
        ),
        **_learning_context(conn, query.id),
        **_pagination(
            repo.count_articles_for_query(
                conn, query.id, query.max_age_years, favorites_only, show_hidden
            ),
            offset=offset,
            url_suite=f"/queries/{query.id}/articles?"
            + _list_params(favorites_only, show_hidden),
        ),
    }


def _pagination(total: int, offset: int, url_suite: str) -> dict[str, Any]:
    """Ce dont `_charger_la_suite.html` a besoin pour proposer la page suivante.

    `offset` est celui de la page rendue ; l'URL de suite pointe la suivante.
    """
    prochain = offset + repo.PAGE_SIZE
    return {
        "total": total,
        "reste": max(total - prochain, 0),
        "url_suite": f"{url_suite}offset={prochain}" if prochain < total else None,
    }


def _panel_and_sidebar(
    request: Request,
    conn: sqlite3.Connection,
    query_id: int,
    report: Any = None,
    favorites_only: bool = False,
    show_hidden: bool = False,
) -> HTMLResponse:
    """Renvoie la liste d'articles et la barre latérale mise à jour."""
    query = repo.get_query(conn, query_id)
    if query is None:
        raise HTTPException(status_code=404, detail="Requête introuvable")
    return templates.TemplateResponse(
        request,
        "_panel_and_sidebar.html",
        {
            **_list_context(
                conn,
                query,
                favorites_only=favorites_only,
                show_hidden=show_hidden,
                offset=0,
            ),
            "report": report,
            **_sidebar_context(conn),
        },
    )


# --- Pages et fragments de lecture ---------------------------------------


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    """Coquille de page : la barre latérale se charge ensuite via HTMX."""
    return templates.TemplateResponse(request, "base.html")


@app.get("/queries", response_class=HTMLResponse)
def sidebar(
    request: Request, conn: sqlite3.Connection = Depends(get_db)
) -> HTMLResponse:
    """Fragment de la barre latérale : dossiers, requêtes, compteurs de non-lus."""
    return templates.TemplateResponse(request, "_sidebar.html", _sidebar_context(conn))


@app.get("/queries/new", response_class=HTMLResponse)
def new_query_form(
    request: Request, conn: sqlite3.Connection = Depends(get_db)
) -> HTMLResponse:
    # Une requête neuve n'a aucune note : le masquage y est d'abord inactif.
    return templates.TemplateResponse(
        request,
        "_query_form.html",
        {
            "query": None,
            "folders": repo.list_folders(conn),
            "learning": relevance.learning_status({}) if relevance_enabled() else None,
            "hide_below": None,
        },
    )


@app.get("/queries/{query_id}/articles", response_class=HTMLResponse)
def query_articles(
    request: Request,
    query_id: int,
    favorites: int = 0,
    offset: int = 0,
    show_hidden: int = 0,
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    """Fragment de liste d'articles, lu depuis le cache.

    Au-delà de la première page, seules les lignes sont rendues : elles se
    greffent à la suite des précédentes.
    """
    query = repo.get_query(conn, query_id)
    if query is None:
        raise HTTPException(status_code=404, detail="Requête introuvable")
    if offset == 0:
        # Ouvrir la requête est ce qui fait avancer le repère du badge
        # « nouveau ». Avant la lecture : le repère décalé désigne alors la
        # session précédente, et les articles arrivés depuis sont bien signalés.
        # Les pages suivantes ne rouvrent rien, elles poursuivent la lecture.
        repo.record_visit(conn, query_id)

    contexte = {
        **_list_context(
            conn,
            query,
            favorites_only=bool(favorites),
            show_hidden=bool(show_hidden),
            offset=offset,
        ),
        "report": None,
    }
    if offset:
        return templates.TemplateResponse(request, "_articles_page.html", contexte)
    return templates.TemplateResponse(
        request,
        "_article_list.html",
        contexte,
    )


@app.get("/queries/{query_id}/edit", response_class=HTMLResponse)
def edit_query_form(
    request: Request, query_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> HTMLResponse:
    query = repo.get_query(conn, query_id)
    if query is None:
        raise HTTPException(status_code=404, detail="Requête introuvable")
    return templates.TemplateResponse(
        request,
        "_query_form.html",
        {
            "query": query,
            "folders": repo.list_folders(conn),
            **_learning_context(conn, query_id),
            "suggestions": relevance.suggestions_for_query(conn, query_id),
        },
    )


# --- Écriture -------------------------------------------------------------


def _save_hide_below(conn: sqlite3.Connection, query_id: int, raw: str | None) -> None:
    """Enregistre le seuil de masquage, champ séparé de la chaîne PubMed.

    Champ absent (couche éteinte, ou menu désactivé tant que le modèle
    apprend) : le réglage en place n'est pas touché. « Ne rien masquer » a donc
    sa propre valeur, `aucun` : FastAPI ramène un champ vide à `None`, et on
    ne le distinguerait plus d'un champ absent.
    """
    if raw is None or not relevance_enabled():
        return
    try:
        repo.set_hide_below(conn, query_id, None if raw == "aucun" else float(raw))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/queries/new", response_class=HTMLResponse)
def create_query(
    request: Request,
    name: str = Form(...),
    pubmed_query: str = Form(...),
    folder: str = Form("Sans dossier"),
    quartile_min: str = Form(""),
    citedness_min: str = Form(""),
    max_age_years: str = Form(""),
    sort: str = Form("date_desc"),
    hide_below: str | None = Form(None),
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    """Enregistre une requête. La chaîne PubMed est stockée telle quelle."""
    query_id = repo.create_query(
        conn,
        name=name.strip(),
        pubmed_query=pubmed_query.strip(),
        folder=folder.strip() or "Sans dossier",
        quartile_min=_optional_number(quartile_min, int),
        citedness_min=_optional_number(citedness_min, float),
        max_age_years=_optional_number(max_age_years, int),
        sort=sort,
    )
    _save_hide_below(conn, query_id, hide_below)
    return _panel_and_sidebar(request, conn, query_id)


@app.post("/queries/{query_id}/edit", response_class=HTMLResponse)
def save_query(
    request: Request,
    query_id: int,
    name: str = Form(...),
    pubmed_query: str = Form(...),
    folder: str = Form("Sans dossier"),
    quartile_min: str = Form(""),
    citedness_min: str = Form(""),
    max_age_years: str = Form(""),
    sort: str = Form("date_desc"),
    hide_below: str | None = Form(None),
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    if repo.get_query(conn, query_id) is None:
        raise HTTPException(status_code=404, detail="Requête introuvable")
    repo.update_query(
        conn,
        query_id,
        name=name.strip(),
        pubmed_query=pubmed_query.strip(),
        folder=folder.strip() or "Sans dossier",
        quartile_min=_optional_number(quartile_min, int),
        citedness_min=_optional_number(citedness_min, float),
        max_age_years=_optional_number(max_age_years, int),
        sort=sort,
    )
    _save_hide_below(conn, query_id, hide_below)
    return _panel_and_sidebar(request, conn, query_id)


@app.delete("/queries/{query_id}", response_class=HTMLResponse)
def remove_query(
    request: Request, query_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> HTMLResponse:
    repo.delete_query(conn, query_id)
    conn.commit()
    return templates.TemplateResponse(request, "_deleted.html", _sidebar_context(conn))


@app.post("/queries/{query_id}/refresh", response_class=HTMLResponse)
async def refresh(
    request: Request,
    query_id: int,
    favorites: int = Form(0),
    show_hidden: int = Form(0),
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    """Rafraîchissement à chaud, à la demande explicite de l'utilisateur."""
    query = repo.get_query(conn, query_id)
    if query is None:
        raise HTTPException(status_code=404, detail="Requête introuvable")
    try:
        async with PubMedClient() as pubmed:
            report = await refresh_query(conn, query, pubmed=pubmed)
    except httpx.HTTPError as exc:
        # Sans cela, la réponse 500 laisse HTMX muet : le clic semble sans effet.
        report = RefreshReport(query_name=query.name)
        report.errors.append(f"PubMed n'a pas répondu : {exc or type(exc).__name__}")
    conn.commit()
    return _panel_and_sidebar(
        request,
        conn,
        query_id,
        report=report,
        favorites_only=bool(favorites),
        show_hidden=bool(show_hidden),
    )


@app.post("/queries/{query_id}/read-all", response_class=HTMLResponse)
def mark_all_read(
    request: Request,
    query_id: int,
    favorites: int = Form(0),
    show_hidden: int = Form(0),
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    """Marque lus tous les articles de la requête courante, et d'elle seule."""
    repo.mark_all_read(conn, query_id, show_hidden=bool(show_hidden))
    conn.commit()
    return _panel_and_sidebar(
        request,
        conn,
        query_id,
        favorites_only=bool(favorites),
        show_hidden=bool(show_hidden),
    )


def _row_and_sidebar(
    request: Request, conn: sqlite3.Connection, article: dict | None
) -> HTMLResponse:
    """Une ligne d'article redessinée, plus la barre latérale hors bande."""
    if article is None:
        raise HTTPException(status_code=404, detail="Article introuvable")
    return templates.TemplateResponse(
        request,
        "_row_and_sidebar.html",
        {"article": article, **_sidebar_context(conn)},
    )


def _panel_row_sidebar(
    request: Request,
    conn: sqlite3.Connection,
    query_id: int,
    pmid: str,
    abstract: str | None,
    error: str | None,
) -> HTMLResponse:
    """Encart de lecture, plus la ligne d'article et la barre latérale hors bande."""
    article = repo.get_article_row(conn, query_id, pmid)
    if article is None:
        raise HTTPException(status_code=404, detail="Article introuvable")
    # Un résumé français déjà en cache s'affiche à l'ouverture, sans clic.
    summary, summary_model = repo.get_summary_fr(conn, pmid)
    return templates.TemplateResponse(
        request,
        "_panel_row_sidebar.html",
        {
            "article": article,
            "abstract": abstract,
            "error": error,
            "summary": summary,
            "summary_model": summary_model,
            "explanation": relevance.explain(conn, query_id, pmid),
            **_sidebar_context(conn),
        },
    )


@app.post("/articles/{pmid}/read", response_class=HTMLResponse)
def toggle_read(
    request: Request,
    pmid: str,
    query_id: int = Form(...),
    read: int = Form(...),
    from_panel: int = Form(0),
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    """Bascule l'état de lecture. Le suivi est propre à chaque requête."""
    repo.set_read(conn, query_id, pmid, read=bool(read))
    conn.commit()
    if from_panel:
        # Appelé depuis l'encart : le résumé y est déjà affiché, donc en cache.
        # Aucun appel à PubMed n'est nécessaire pour le redessiner.
        return _panel_row_sidebar(
            request, conn, query_id, pmid, repo.get_abstract(conn, pmid), None
        )
    return _row_and_sidebar(request, conn, repo.get_article_row(conn, query_id, pmid))


async def _fill_missing_citations(conn: sqlite3.Connection) -> None:
    """Complète en un seul lot les favoris sans métadonnées de citation.

    Même modèle que le résumé : un geste explicite (poser un favori, ouvrir
    la bibliographie) déclenche l'appel, le résultat est mis en cache.
    `authors` non nul (chaîne vide comprise) signifie « déjà interrogé ».
    Un échec réseau reste silencieux — le favori vient d'être posé et doit le
    rester, la bibliographie s'affiche avec ce qu'on a.
    """
    manquants = repo.favorites_missing_citation(conn)
    if not manquants:
        return
    try:
        async with PubMedClient() as pubmed:
            fetched = await pubmed.esummary(manquants)
    except httpx.HTTPError as exc:
        logging.getLogger(__name__).warning(
            "Métadonnées de citation indisponibles pour %d favoris : %s",
            len(manquants),
            exc,
        )
        return
    for summary in fetched:
        repo.upsert_article(conn, summary)
    conn.commit()


@app.post("/articles/{pmid}/favorite", response_class=HTMLResponse)
async def toggle_favorite(
    request: Request,
    pmid: str,
    favorite: int = Form(...),
    query_id: int | None = Form(None),
    from_panel: int = Form(0),
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    """Ajoute ou retire l'article de la bibliographie. Global à l'article."""
    if repo.get_article_summary(conn, pmid) is None:
        raise HTTPException(status_code=404, detail="Article introuvable")
    repo.set_favorite(conn, pmid, favorite=bool(favorite))
    conn.commit()
    if favorite:
        await _fill_missing_citations(conn)

    if from_panel and query_id:
        return _panel_row_sidebar(
            request, conn, query_id, pmid, repo.get_abstract(conn, pmid), None
        )
    article = (
        repo.get_article_row(conn, query_id, pmid)
        if query_id
        else repo.get_article_row_anywhere(conn, pmid)
    )
    return _row_and_sidebar(request, conn, article)


@app.post("/articles/{pmid}/rating", response_class=HTMLResponse)
def rate_article(
    request: Request,
    pmid: str,
    query_id: int = Form(...),
    rating: int = Form(...),
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    """Note l'article de 1 à 3 dans cette requête ; 0 retire la note.

    Appelé depuis l'encart : le résumé y est déjà affiché, donc en cache.
    """
    if not relevance_enabled():
        raise HTTPException(status_code=404, detail="Notation désactivée")
    if rating != 0 and rating not in repo.RATINGS:
        raise HTTPException(status_code=422, detail="Note attendue : 0, 1, 2 ou 3")
    if repo.get_article_row(conn, query_id, pmid) is None:
        raise HTTPException(status_code=404, detail="Article introuvable")
    if rating:
        repo.set_rating(conn, query_id, pmid, rating)
    else:
        repo.clear_rating(conn, query_id, pmid)
    # Chaque note réentraîne le modèle de la requête : les badges de la liste
    # suivront à son prochain affichage.
    relevance.recompute_scores(conn, query_id)
    conn.commit()
    return _panel_row_sidebar(
        request, conn, query_id, pmid, repo.get_abstract(conn, pmid), None
    )


def _new_context(conn: sqlite3.Connection, offset: int = 0) -> dict[str, Any]:
    """Contexte du flux, partagé par l'affichage, la purge et les pages."""
    return {
        "articles": repo.list_new_articles(conn, limit=repo.PAGE_SIZE, offset=offset),
        **_pagination(repo.count_new_articles(conn), offset=offset, url_suite="/new?"),
    }


@app.get("/new", response_class=HTMLResponse)
def new_articles(
    request: Request, offset: int = 0, conn: sqlite3.Connection = Depends(get_db)
) -> HTMLResponse:
    """Le flux : tout ce qui est arrivé depuis la dernière visite.

    Consulter le flux ne consomme rien — sans quoi l'ouvrir le viderait. Seules
    l'ouverture d'une requête et le bouton « Tout marquer comme vu » décalent
    les repères.
    """
    gabarit = "_new_page.html" if offset else "_new_list.html"
    return templates.TemplateResponse(request, gabarit, _new_context(conn, offset))


@app.post("/new/dismiss", response_class=HTMLResponse)
def dismiss_new(
    request: Request, conn: sqlite3.Connection = Depends(get_db)
) -> HTMLResponse:
    """Vide le flux et rend la barre latérale à jour dans la foulée."""
    repo.dismiss_new_articles(conn)
    conn.commit()
    return templates.TemplateResponse(
        request,
        "_new_and_sidebar.html",
        {**_new_context(conn), **_sidebar_context(conn)},
    )


@app.get("/search", response_class=HTMLResponse)
def search(
    request: Request,
    q: str = "",
    offset: int = 0,
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    """Recherche dans le cache local. Aucun appel réseau : c'est notre base."""
    saisie = q.strip()
    contexte = {
        "saisie": saisie,
        "articles": repo.search_articles(
            conn, saisie, limit=repo.PAGE_SIZE, offset=offset
        ),
        **_pagination(
            repo.count_search_results(conn, saisie),
            offset=offset,
            url_suite=f"/search?q={quote_plus(saisie)}&",
        ),
    }
    gabarit = "_search_page.html" if offset else "_search_results.html"
    return templates.TemplateResponse(request, gabarit, contexte)


@app.get("/favorites", response_class=HTMLResponse)
async def favorites(
    request: Request, conn: sqlite3.Connection = Depends(get_db)
) -> HTMLResponse:
    """La bibliographie : tous les favoris, avec leur rendu Vancouver."""
    await _fill_missing_citations(conn)
    articles = repo.list_favorite_articles(conn)
    return templates.TemplateResponse(
        request,
        "_favorites.html",
        {"articles": articles, "bibliography": citation.to_bibliography(articles)},
    )


@app.get("/favorites/export.bib")
async def export_bibtex(conn: sqlite3.Connection = Depends(get_db)) -> Response:
    """Fichier BibTeX des favoris, prêt pour Zotero, Mendeley ou LaTeX."""
    await _fill_missing_citations(conn)
    contenu = citation.to_bibtex(repo.list_favorite_articles(conn))
    return Response(
        contenu,
        media_type="application/x-bibtex; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="favoris.bib"'},
    )


# --- Appels délégués à PubMed --------------------------------------------


async def _load_abstract(
    conn: sqlite3.Connection, pmid: str
) -> tuple[str | None, str | None]:
    """Résumé depuis le cache, sinon efetch. Renvoie (résumé, erreur).

    Le rafraîchissement ne récupère jamais les résumés ; c'est ici, et
    seulement ici, qu'ils entrent en base.
    """
    abstract = repo.get_abstract(conn, pmid)
    if abstract is not None:
        return abstract, None
    try:
        async with PubMedClient() as pubmed:
            abstract = await pubmed.efetch_abstract(pmid)
    except httpx.HTTPError as exc:
        return None, str(exc) or exc.__class__.__name__
    # Un article sans résumé (éditorial, lettre) est mis en cache comme chaîne
    # vide : inutile de rappeler PubMed à chaque ouverture.
    repo.set_abstract(conn, pmid, abstract or "")
    conn.commit()
    return abstract, None


@app.get("/articles/{pmid}/panel", response_class=HTMLResponse)
async def article_panel(
    request: Request,
    pmid: str,
    query_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    """Ouvre un article dans l'encart de lecture — et le compte donc pour lu."""
    if repo.get_article_summary(conn, pmid) is None:
        raise HTTPException(status_code=404, detail="Article introuvable")
    repo.set_read(conn, query_id, pmid, read=True)
    conn.commit()
    abstract, error = await _load_abstract(conn, pmid)
    return _panel_row_sidebar(request, conn, query_id, pmid, abstract, error)


def _render_summary(
    request: Request,
    pmid: str,
    *,
    summary: str | None = None,
    summary_model: str | None = None,
    error: str | None = None,
    no_abstract: bool = False,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_summary_fr.html",
        {
            "pmid": pmid,
            "summary": summary,
            "summary_model": summary_model,
            "error": error,
            "no_abstract": no_abstract,
        },
    )


@app.post("/articles/{pmid}/summary", response_class=HTMLResponse)
async def article_summary(
    request: Request,
    pmid: str,
    regenerate: int = Form(0),
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    """Résumé français produit par le LLM local, mis en cache comme l'abstract.

    Même modèle que le reste de l'application : on ne régénère que sur demande
    explicite, la base répond pour tout le reste.
    """
    article = repo.get_article_summary(conn, pmid)
    if article is None:
        raise HTTPException(status_code=404, detail="Article introuvable")

    if not regenerate:
        cached, model = repo.get_summary_fr(conn, pmid)
        if cached:
            return _render_summary(request, pmid, summary=cached, summary_model=model)

    abstract, error = await _load_abstract(conn, pmid)
    if error is not None:
        return _render_summary(request, pmid, error=f"PubMed n'a pas répondu : {error}")
    if not abstract:
        return _render_summary(request, pmid, no_abstract=True)

    try:
        async with OllamaClient() as llm:
            generated = await llm.summarize(article.title, abstract)
    except httpx.HTTPError as exc:
        detail = str(exc) or exc.__class__.__name__
        return _render_summary(
            request,
            pmid,
            error=f"Le modèle local n'a pas répondu : {detail}",
        )

    if not generated.text:
        return _render_summary(
            request, pmid, error="Le modèle local a renvoyé une réponse vide."
        )

    if generated.truncated:
        return _render_summary(
            request,
            pmid,
            summary=generated.text,
            summary_model=generated.model,
            error="Résumé coupé par le plafond de longueur : non mis en cache.",
        )

    repo.set_summary_fr(conn, pmid, generated.text, generated.model)
    conn.commit()
    return _render_summary(
        request, pmid, summary=generated.text, summary_model=generated.model
    )


@app.get("/preview", response_class=HTMLResponse)
async def preview(request: Request, pubmed_query: str = "") -> HTMLResponse:
    """Aperçu live : nombre de résultats, traduction et avertissements PubMed.

    C'est notre seule validation de la syntaxe. Aucune analyse locale.
    """
    term = pubmed_query.strip()
    context: dict[str, Any] = {"term": term, "result": None, "error": None}
    if term:
        context["pubmed_url"] = PUBMED_SEARCH_URL + quote_plus(term)
        error_message: str | None = None
        try:
            async with PubMedClient() as pubmed:
                # retmax=0 : seuls le décompte et la traduction nous intéressent.
                # reldate_days=0 : pas de fenêtre de récence, pour que le chiffre
                # corresponde à ce que montre le lien « ouvrir dans PubMed ».
                context["result"] = await pubmed.esearch(term, retmax=0, reldate_days=0)
        except httpx.HTTPError as exc:
            error_message = str(exc) or exc.__class__.__name__
        context["error"] = error_message
    return templates.TemplateResponse(request, "_preview.html", context)


@app.get("/journals/resolve", response_class=HTMLResponse)
async def resolve_journal(request: Request, q: str = "") -> HTMLResponse:
    """Cherche l'abréviation NLM d'une revue (piège NEJM → N Engl J Med)."""
    term = q.strip()
    matches = []
    if len(term) >= 3:
        try:
            async with PubMedClient() as pubmed:
                matches = await pubmed.resolve_journal(term)
        except httpx.HTTPError:
            matches = []
    return templates.TemplateResponse(
        request, "_journal_matches.html", {"matches": matches, "searched": bool(term)}
    )
