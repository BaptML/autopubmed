"""Application FastAPI.

L'interface ne lit que la base : aucun appel PubMed n'est déclenché par une
page. Toutes les réponses sont des fragments HTML consommés par HTMX.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import repo
from app.config import PROJECT_ROOT
from app.db import connect, init_db

TEMPLATES_DIR = PROJECT_ROOT / "app" / "templates"
STATIC_DIR = PROJECT_ROOT / "app" / "static"

_MOIS = ["", "01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12"]


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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
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


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    """Coquille de page : la barre latérale se charge ensuite via HTMX."""
    return templates.TemplateResponse(request, "base.html")


@app.get("/queries", response_class=HTMLResponse)
async def sidebar(
    request: Request, conn: sqlite3.Connection = Depends(get_db)
) -> HTMLResponse:
    """Fragment de la barre latérale : dossiers, requêtes, compteurs de non-lus."""
    return templates.TemplateResponse(
        request, "_sidebar.html", {"folders": repo.folders_with_queries(conn)}
    )


@app.get("/queries/{query_id}/articles", response_class=HTMLResponse)
async def query_articles(
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
            "articles": repo.list_articles_for_query(conn, query_id, query.sort),
        },
    )
