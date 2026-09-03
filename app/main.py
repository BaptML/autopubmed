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

from app import citation, repo
from app.config import PROJECT_ROOT
from app.db import connect, init_db
from app.pubmed import PubMedClient
from app.refresh import refresh_query
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


app = FastAPI(title="Veille PubMed", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.filters["date_fr"] = date_fr
templates.env.filters["date_courte"] = date_courte


def get_db() -> Iterator[sqlite3.Connection]:
    """Une connexion par requête HTTP, fermée à la fin de celle-ci."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _optional_int(raw: str | None) -> int | None:
    """Un champ de formulaire vide vaut « pas de filtre », pas zéro."""
    raw = (raw or "").strip()
    return int(raw) if raw else None


def _optional_float(raw: str | None) -> float | None:
    raw = (raw or "").strip().replace(",", ".")
    return float(raw) if raw else None


def _sidebar_context(conn: sqlite3.Connection) -> dict[str, Any]:
    """Ce dont `_sidebar.html` a besoin, où qu'il soit inclus."""
    return {
        "folders": repo.folders_with_queries(conn),
        "favorites_count": repo.count_favorites(conn),
        "new_count": repo.count_new_articles(conn),
    }


def _panel_and_sidebar(
    request: Request,
    conn: sqlite3.Connection,
    query_id: int,
    report: Any = None,
    favorites_only: bool = False,
) -> HTMLResponse:
    """Renvoie la liste d'articles et la barre latérale mise à jour."""
    query = repo.get_query(conn, query_id)
    if query is None:
        raise HTTPException(status_code=404, detail="Requête introuvable")
    return templates.TemplateResponse(
        request,
        "_panel_and_sidebar.html",
        {
            "query": query,
            "articles": repo.list_articles_for_query(
                conn,
                query_id,
                query.sort,
                max_age_years=query.max_age_years,
                favorites_only=favorites_only,
            ),
            "favorites_only": favorites_only,
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
    return templates.TemplateResponse(
        request, "_sidebar.html", _sidebar_context(conn)
    )


@app.get("/queries/new", response_class=HTMLResponse)
def new_query_form(
    request: Request, conn: sqlite3.Connection = Depends(get_db)
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_query_form.html",
        {"query": None, "folders": repo.list_folders(conn)},
    )


@app.get("/queries/{query_id}/articles", response_class=HTMLResponse)
def query_articles(
    request: Request,
    query_id: int,
    favorites: int = 0,
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    """Fragment de liste d'articles, lu depuis le cache."""
    query = repo.get_query(conn, query_id)
    if query is None:
        raise HTTPException(status_code=404, detail="Requête introuvable")
    # Ouvrir la requête est ce qui fait avancer le repère du badge « nouveau ».
    # Avant la lecture : le repère décalé désigne alors la session précédente,
    # et les articles arrivés depuis s'affichent bien comme nouveaux.
    repo.record_visit(conn, query_id)
    return templates.TemplateResponse(
        request,
        "_article_list.html",
        {
            "query": query,
            "articles": repo.list_articles_for_query(
                conn,
                query_id,
                query.sort,
                max_age_years=query.max_age_years,
                favorites_only=bool(favorites),
            ),
            "favorites_only": bool(favorites),
            "report": None,
        },
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
        {"query": query, "folders": repo.list_folders(conn)},
    )


# --- Écriture -------------------------------------------------------------


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
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    """Enregistre une requête. La chaîne PubMed est stockée telle quelle."""
    query_id = repo.create_query(
        conn,
        name=name.strip(),
        pubmed_query=pubmed_query.strip(),
        folder=folder.strip() or "Sans dossier",
        quartile_min=_optional_int(quartile_min),
        citedness_min=_optional_float(citedness_min),
        max_age_years=_optional_int(max_age_years),
        sort=sort,
    )
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
        quartile_min=_optional_int(quartile_min),
        citedness_min=_optional_float(citedness_min),
        max_age_years=_optional_int(max_age_years),
        sort=sort,
    )
    return _panel_and_sidebar(request, conn, query_id)


@app.delete("/queries/{query_id}", response_class=HTMLResponse)
def remove_query(
    request: Request, query_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> HTMLResponse:
    repo.delete_query(conn, query_id)
    conn.commit()
    return templates.TemplateResponse(
        request, "_deleted.html", _sidebar_context(conn)
    )


@app.post("/queries/{query_id}/refresh", response_class=HTMLResponse)
async def refresh(
    request: Request,
    query_id: int,
    favorites: int = Form(0),
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    """Rafraîchissement à chaud, à la demande explicite de l'utilisateur."""
    query = repo.get_query(conn, query_id)
    if query is None:
        raise HTTPException(status_code=404, detail="Requête introuvable")
    async with PubMedClient() as pubmed:
        report = await refresh_query(conn, query, pubmed=pubmed)
    conn.commit()
    return _panel_and_sidebar(
        request, conn, query_id, report=report, favorites_only=bool(favorites)
    )


@app.post("/queries/{query_id}/read-all", response_class=HTMLResponse)
def mark_all_read(
    request: Request,
    query_id: int,
    favorites: int = Form(0),
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    """Marque lus tous les articles de la requête courante, et d'elle seule."""
    repo.mark_all_read(conn, query_id)
    conn.commit()
    return _panel_and_sidebar(
        request, conn, query_id, favorites_only=bool(favorites)
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
    return templates.TemplateResponse(
        request,
        "_panel_row_sidebar.html",
        {
            "article": article,
            "abstract": abstract,
            "error": error,
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
    article = repo.get_article_row(conn, query_id, pmid)
    if article is None:
        raise HTTPException(status_code=404, detail="Article introuvable")
    return templates.TemplateResponse(
        request,
        "_row_and_sidebar.html",
        {"article": article, **_sidebar_context(conn)},
    )


async def _load_citation(conn: sqlite3.Connection, pmid: str) -> None:
    """Complète les métadonnées de citation d'un article, si elles manquent.

    Même modèle que le résumé : un geste explicite de l'utilisateur déclenche
    l'appel, le résultat est mis en cache. `authors` non nul (chaîne vide
    comprise) signifie « déjà interrogé » : on ne rappelle pas PubMed.

    L'échec réseau n'est pas remonté : le favori vient d'être posé et doit le
    rester ; l'export rattrapera les métadonnées manquantes.
    """
    summary = repo.get_article_summary(conn, pmid)
    if summary is None or summary.authors is not None:
        return
    try:
        async with PubMedClient() as pubmed:
            fetched = await pubmed.esummary([pmid])
    except httpx.HTTPError as exc:
        logging.getLogger(__name__).warning(
            "Métadonnées de citation indisponibles pour %s : %s", pmid, exc
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
        await _load_citation(conn, pmid)

    if from_panel and query_id:
        return _panel_row_sidebar(
            request, conn, query_id, pmid, repo.get_abstract(conn, pmid), None
        )
    article = (
        repo.get_article_row(conn, query_id, pmid)
        if query_id
        else repo.get_article_row_anywhere(conn, pmid)
    )
    if article is None:
        raise HTTPException(status_code=404, detail="Article introuvable")
    return templates.TemplateResponse(
        request,
        "_row_and_sidebar.html",
        {"article": article, **_sidebar_context(conn)},
    )


async def _fill_missing_citations(conn: sqlite3.Connection) -> None:
    """Complète en un seul lot les favoris sans métadonnées de citation.

    Ne concerne que les articles entrés en base avant l'arrivée des colonnes :
    la bibliographie serait sinon amputée de leurs auteurs. Un échec réseau
    reste silencieux — la bibliographie s'affiche avec ce qu'on a.
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


@app.get("/new", response_class=HTMLResponse)
def new_articles(
    request: Request, conn: sqlite3.Connection = Depends(get_db)
) -> HTMLResponse:
    """Le flux : tout ce qui est arrivé depuis la dernière visite.

    Consulter le flux ne consomme rien — sans quoi l'ouvrir le viderait. Seules
    l'ouverture d'une requête et le bouton « Tout marquer comme vu » décalent
    les repères.
    """
    return templates.TemplateResponse(
        request,
        "_new_list.html",
        {"articles": repo.list_new_articles(conn)},
    )


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
        {"articles": repo.list_new_articles(conn), **_sidebar_context(conn)},
    )


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
