"""Application FastAPI.

L'interface lit la base ; les seuls appels sortants sont l'aperçu live, le
résolveur de revue et le bouton « Rafraîchir » — tous déclenchés
explicitement par l'utilisateur. Toutes les réponses sont des fragments HTML
consommés par HTMX.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote_plus

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import repo
from app.config import PROJECT_ROOT
from app.db import connect, init_db
from app.pubmed import PubMedClient
from app.refresh import refresh_query

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


_UNSET = object()  # frontière du « nouveau » pas encore déterminée


@asynccontextmanager
async def lifespan(app_: FastAPI):
    init_db()
    # La frontière est fixée à la première requête servie, avec la connexion de
    # celle-ci (voir _new_since), puis figée pour toute la session.
    app_.state.new_since = _UNSET
    yield


app = FastAPI(title="Veille PubMed", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.filters["date_fr"] = date_fr


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


def _new_since(request: Request, conn: sqlite3.Connection) -> str | None:
    """Frontière du « nouveau » : l'ouverture précédente de l'application.

    Déterminée une seule fois, à la première requête servie, pour que le
    signalement ne se déplace pas sous les yeux de l'utilisateur pendant sa
    session. Vaut None au tout premier lancement.
    """
    state = request.app.state
    if getattr(state, "new_since", _UNSET) is _UNSET:
        state.new_since = repo.record_app_opening(conn)
        conn.commit()
    return state.new_since


def _panel_and_sidebar(
    request: Request, conn: sqlite3.Connection, query_id: int, report: Any = None
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
                conn, query_id, query.sort, new_since=_new_since(request, conn)
            ),
            "folders": repo.folders_with_queries(conn),
            "report": report,
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
        request, "_sidebar.html", {"folders": repo.folders_with_queries(conn)}
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
    request: Request, query_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> HTMLResponse:
    """Fragment de liste d'articles, lu depuis le cache."""
    query = repo.get_query(conn, query_id)
    if query is None:
        raise HTTPException(status_code=404, detail="Requête introuvable")
    return templates.TemplateResponse(
        request,
        "_article_list.html",
        {
            "query": query,
            "articles": repo.list_articles_for_query(
                conn, query_id, query.sort, new_since=_new_since(request, conn)
            ),
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
        request, "_deleted.html", {"folders": repo.folders_with_queries(conn)}
    )


@app.post("/queries/{query_id}/refresh", response_class=HTMLResponse)
async def refresh(
    request: Request, query_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> HTMLResponse:
    """Rafraîchissement à chaud, à la demande explicite de l'utilisateur."""
    query = repo.get_query(conn, query_id)
    if query is None:
        raise HTTPException(status_code=404, detail="Requête introuvable")
    async with PubMedClient() as pubmed:
        report = await refresh_query(conn, query, pubmed=pubmed)
    conn.commit()
    return _panel_and_sidebar(request, conn, query_id, report=report)


@app.post("/articles/{pmid}/read", response_class=HTMLResponse)
def toggle_read(
    request: Request,
    pmid: str,
    query_id: int = Form(...),
    read: int = Form(...),
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    """Bascule l'état de lecture. Le suivi est propre à chaque requête."""
    repo.set_read(conn, query_id, pmid, read=bool(read))
    conn.commit()
    article = repo.get_article_row(conn, query_id, pmid, new_since=_new_since(request, conn))
    if article is None:
        raise HTTPException(status_code=404, detail="Article introuvable")
    return templates.TemplateResponse(
        request,
        "_row_and_sidebar.html",
        {"article": article, "folders": repo.folders_with_queries(conn)},
    )


# --- Appels délégués à PubMed --------------------------------------------


@app.get("/articles/{pmid}/abstract", response_class=HTMLResponse)
async def article_abstract(
    request: Request, pmid: str, conn: sqlite3.Connection = Depends(get_db)
) -> HTMLResponse:
    """Résumé à la demande : efetch au premier dépliage, puis cache.

    Le rafraîchissement ne récupère jamais les résumés ; c'est ici, et
    seulement ici, qu'ils entrent en base.
    """
    if repo.get_article_summary(conn, pmid) is None:
        raise HTTPException(status_code=404, detail="Article introuvable")

    abstract = repo.get_abstract(conn, pmid)
    error: str | None = None
    if abstract is None:
        try:
            async with PubMedClient() as pubmed:
                abstract = await pubmed.efetch_abstract(pmid)
        except httpx.HTTPError as exc:
            error = str(exc) or exc.__class__.__name__
        else:
            # Un article sans résumé (éditorial, lettre) est mis en cache comme
            # chaîne vide : inutile de rappeler PubMed à chaque dépliage.
            repo.set_abstract(conn, pmid, abstract or "")
            conn.commit()

    return templates.TemplateResponse(
        request,
        "_abstract.html",
        {"pmid": pmid, "abstract": abstract, "error": error},
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
