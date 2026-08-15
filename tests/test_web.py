"""Tests des fragments HTML servis à HTMX, sur une base temporaire."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import repo
from app.db import connect, get_conn, init_db
from app.main import app, date_fr, get_db
from app.models import ArticleSummary


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    db_path = tmp_path / "web.db"
    init_db(db_path)

    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO journal_quality (issn, journal_name, quartile, source, updated_at)"
            " VALUES ('00284793', 'NEJM', 1, 'scimago', '2026-01-01T00:00:00+00:00')"
        )
        query_id = repo.create_query(
            conn,
            name="NEJM / essais randomisés",
            folder="Références",
            pubmed_query='"N Engl J Med"[ta]',
            quartile_min=1,
        )
        repo.create_query(conn, name="Requête vide", folder="Brouillons", pubmed_query="zzz")
        repo.upsert_article(
            conn,
            ArticleSummary(
                pmid="40000001",
                title="Semaglutide in Heart Failure",
                journal="N Engl J Med",
                issn="00284793",
                eissn=None,
                pub_date="2026-08-13",
            ),
        )
        repo.link_article(conn, query_id, "40000001")
        repo.mark_refreshed(conn, query_id)

    def override() -> Iterator:
        conn = connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as test_client:
        test_client.query_id = query_id  # type: ignore[attr-defined]
        yield test_client
    app.dependency_overrides.clear()


def test_page_d_accueil_est_une_coquille(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    # La liste n'est pas rendue côté serveur : HTMX ira la chercher.
    assert 'hx-get="/queries"' in response.text
    assert "Semaglutide" not in response.text


def test_sidebar_groupe_par_dossier_et_compte_les_non_lus(client: TestClient) -> None:
    response = client.get("/queries")
    assert response.status_code == 200
    assert "Références" in response.text
    assert "Brouillons" in response.text
    assert "NEJM / essais randomisés" in response.text
    # Un article non lu sur la première requête, zéro sur la seconde.
    assert '<span class="compteur ">1</span>' in response.text
    assert '<span class="compteur vide">0</span>' in response.text


def test_liste_d_articles_affiche_titre_revue_et_badge(client: TestClient) -> None:
    response = client.get(f"/queries/{client.query_id}/articles")
    assert response.status_code == 200
    assert "Semaglutide in Heart Failure" in response.text
    assert "N Engl J Med" in response.text
    assert 'class="badge q1"' in response.text
    assert "13/08/2026" in response.text
    assert "https://pubmed.ncbi.nlm.nih.gov/40000001/" in response.text


def test_liste_vide_explique_comment_la_remplir(client: TestClient) -> None:
    response = client.get("/queries/2/articles")
    assert response.status_code == 200
    assert "Aucun article en cache" in response.text


def test_requete_inexistante_renvoie_404(client: TestClient) -> None:
    assert client.get("/queries/999/articles").status_code == 404


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-08-13", "13/08/2026"),
        ("2026-08", "08/2026"),  # date PubMed partielle
        ("2026", "2026"),
        ("2026-08-15T21:30:00+00:00", "15/08/2026 à 21:30"),
        (None, "date inconnue"),
    ],
)
def test_date_fr(value: str | None, expected: str) -> None:
    assert date_fr(value) == expected
