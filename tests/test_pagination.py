"""Tests de la pagination des trois listes : requête, flux, recherche."""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import repo
from app.db import connect, get_conn, init_db
from app.main import app, get_db
from app.models import ArticleSummary

# Assez d'articles pour dépasser deux pages, sans alourdir la suite.
TOTAL = repo.PAGE_SIZE * 2 + 7


def _il_y_a(minutes: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return moment.replace(microsecond=0).isoformat()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """Une requête déjà visitée, portant TOTAL articles tous nouveaux."""
    db_path = tmp_path / "pagination.db"
    init_db(db_path)

    with get_conn(db_path) as conn:
        query_id = repo.create_query(conn, name="Large", pubmed_query="x")
        conn.execute(
            "UPDATE queries SET last_visited_at = ?, previous_visit_at = ?",
            (_il_y_a(minutes=600), _il_y_a(minutes=600)),
        )
        for i in range(TOTAL):
            repo.upsert_article(
                conn,
                ArticleSummary(
                    pmid=f"{40000000 + i}",
                    title=f"Sepsis study number {i}",
                    journal="N Engl J Med",
                    issn="00284793",
                    eissn=None,
                    pub_date=f"2026-01-{(i % 28) + 1:02d}",
                ),
            )
            repo.link_article(conn, query_id, f"{40000000 + i}")
        conn.execute(
            "UPDATE query_articles SET first_seen = ?", (_il_y_a(minutes=30),)
        )

    def override() -> Iterator:
        conn = connect(db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    app.dependency_overrides[get_db] = override
    app.state.db_path = db_path
    with TestClient(app) as test_client:
        test_client.query_id = query_id  # type: ignore[attr-defined]
        test_client.db_path = db_path  # type: ignore[attr-defined]
        yield test_client
    app.dependency_overrides.clear()
    del app.state.db_path


def _lignes(page: str) -> int:
    return page.count('<article class="article')


def _url_de_suite(page: str) -> str | None:
    """URL du bouton « Charger la suite », s'il y en a un.

    On repart du bouton et non du premier `hx-get` venu : chaque ligne
    d'article porte le sien, vers l'encart de lecture. Les entités HTML sont
    décodées, un attribut portant « &amp; » là où l'URL a « & ».
    """
    marqueur = 'class="bouton mono charger-suite"'
    if marqueur not in page:
        return None
    brut = page.split(marqueur, 1)[1].split('hx-get="', 1)[1].split('"', 1)[0]
    return unescape(brut)


def _tout_charger(client: TestClient, url: str) -> int:
    """Suit les boutons « Charger la suite » et renvoie le total des lignes.

    Le nombre de pages est borné : une suite qui se répéterait doit faire
    échouer le test, pas figer la suite entière.
    """
    page = client.get(url).text
    lignes = _lignes(page)
    for _ in range(TOTAL // repo.PAGE_SIZE + 2):
        suite = _url_de_suite(page)
        if suite is None:
            return lignes
        page = client.get(suite).text
        lignes += _lignes(page)
    raise AssertionError(f"pagination sans fin sur {url}")


# --- Liste d'une requête --------------------------------------------------


def test_la_liste_ne_rend_qu_une_page(client: TestClient) -> None:
    page = client.get(f"/queries/{client.query_id}/articles").text
    assert _lignes(page) == repo.PAGE_SIZE


def test_la_liste_annonce_le_total_et_non_la_page(client: TestClient) -> None:
    page = client.get(f"/queries/{client.query_id}/articles").text
    assert f"{TOTAL} article" in page
    assert f"Charger la suite — {TOTAL - repo.PAGE_SIZE} articles restants" in page


def test_la_liste_se_charge_entierement_de_proche_en_proche(client: TestClient) -> None:
    assert _tout_charger(client, f"/queries/{client.query_id}/articles") == TOTAL


def test_la_derniere_page_n_offre_plus_de_suite(client: TestClient) -> None:
    offset = repo.PAGE_SIZE * 2
    page = client.get(f"/queries/{client.query_id}/articles?offset={offset}").text
    assert _lignes(page) == TOTAL - offset
    assert "charger-suite" not in page


def test_une_page_suivante_ne_rend_que_les_lignes(client: TestClient) -> None:
    """Elle se greffe dans la liste : ni en-tête, ni encart de lecture."""
    page = client.get(f"/queries/{client.query_id}/articles?offset=50").text
    assert "entete-liste" not in page
    assert 'id="encart"' not in page


def test_les_pages_ne_se_recouvrent_pas(client: TestClient) -> None:
    premiere = client.get(f"/queries/{client.query_id}/articles").text
    seconde = client.get(f"/queries/{client.query_id}/articles?offset=50").text
    pmids = lambda p: {  # noqa: E731
        bloc.split('"', 1)[0] for bloc in p.split('id="article-')[1:]
    }
    assert not (pmids(premiere) & pmids(seconde))


def test_une_page_suivante_ne_decale_pas_le_repere(client: TestClient) -> None:
    """Poursuivre la lecture n'est pas rouvrir la requête."""
    client.get(f"/queries/{client.query_id}/articles")
    with get_conn(client.db_path) as conn:
        avant = conn.execute("SELECT previous_visit_at FROM queries").fetchone()[0]

    client.get(f"/queries/{client.query_id}/articles?offset=50")
    with get_conn(client.db_path) as conn:
        apres = conn.execute("SELECT previous_visit_at FROM queries").fetchone()[0]
    assert apres == avant


def test_le_compteur_de_non_lus_porte_sur_toute_la_requete(client: TestClient) -> None:
    """Compté sur la page affichée, il aurait plafonné à PAGE_SIZE."""
    page = client.get(f"/queries/{client.query_id}/articles").text
    assert f"Marquer les {TOTAL} articles non lus" in page


# --- Flux -----------------------------------------------------------------


def test_le_flux_ne_rend_qu_une_page(client: TestClient) -> None:
    assert _lignes(client.get("/new").text) == repo.PAGE_SIZE


def test_le_flux_se_charge_entierement(client: TestClient) -> None:
    assert _tout_charger(client, "/new") == TOTAL


def test_chaque_page_du_flux_porte_les_provenances(client: TestClient) -> None:
    page = client.get("/new?offset=50").text
    assert page.count('class="provenance') == repo.PAGE_SIZE


def test_le_flux_annonce_son_total(client: TestClient) -> None:
    assert f"les {TOTAL} nouveautés comme vues" in client.get("/new").text


# --- Recherche ------------------------------------------------------------


def test_la_recherche_ne_rend_qu_une_page(client: TestClient) -> None:
    assert _lignes(client.get("/search?q=sepsis").text) == repo.PAGE_SIZE


def test_la_recherche_se_charge_entierement(client: TestClient) -> None:
    assert _tout_charger(client, "/search?q=sepsis") == TOTAL


def test_la_suite_de_recherche_conserve_la_saisie(client: TestClient) -> None:
    page = client.get("/search?q=sepsis study").text
    assert "q=sepsis+study" in page


# --- Dépôt ----------------------------------------------------------------


def test_sans_limite_le_depot_rend_tout(client: TestClient) -> None:
    """La pagination est un choix de l'appelant, pas un plafond imposé."""
    with get_conn(client.db_path) as conn:
        assert len(repo.list_articles_for_query(conn, client.query_id)) == TOTAL


def test_le_decalage_avance_dans_la_liste(client: TestClient) -> None:
    with get_conn(client.db_path) as conn:
        tout = repo.list_articles_for_query(conn, client.query_id)
        page = repo.list_articles_for_query(
            conn, client.query_id, limit=10, offset=20
        )
    assert [a["pmid"] for a in page] == [a["pmid"] for a in tout[20:30]]
