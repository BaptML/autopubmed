"""Notation manuelle de 1 à 3 : encart, ligne, propreté par requête, drapeau."""

import dataclasses
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, repo
from app.db import RELEVANCE_TABLES, connect, get_conn, init_db
from app.main import app, get_db
from app.models import ArticleSummary

PMID = "40000001"


def _desactiver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        db, "settings", dataclasses.replace(db.settings, enable_relevance=False)
    )


def _client(db_path: Path) -> Iterator[TestClient]:
    with get_conn(db_path) as conn:
        ids = [
            repo.create_query(conn, name=nom, pubmed_query=nom)
            for nom in ("Sepsis", "Ventilation")
        ]
        repo.upsert_article(
            conn,
            ArticleSummary(
                pmid=PMID,
                title="Early vasopressors in septic shock",
                journal="N Engl J Med",
                issn="00284793",
                eissn=None,
                pub_date="2026-08-13",
            ),
        )
        for query_id in ids:
            repo.link_article(conn, query_id, PMID)

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
        test_client.query_ids = ids  # type: ignore[attr-defined]
        test_client.db_path = db_path  # type: ignore[attr-defined]
        yield test_client
    app.dependency_overrides.clear()
    del app.state.db_path


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    db_path = tmp_path / "web.db"
    init_db(db_path)
    yield from _client(db_path)


@pytest.fixture
def client_sans_pertinence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Drapeau à 0 sur une base qui n'a jamais eu les tables : l'état « avant »."""
    _desactiver(monkeypatch)
    db_path = tmp_path / "web.db"
    init_db(db_path)
    with get_conn(db_path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert not set(RELEVANCE_TABLES) & tables
    yield from _client(db_path)


def _noter(client: TestClient, rating: int, query_index: int = 0):
    return client.post(
        f"/articles/{PMID}/rating",
        data={"query_id": client.query_ids[query_index], "rating": rating},
    )


def _note_en_base(client: TestClient, query_index: int = 0) -> int | None:
    with get_conn(client.db_path) as conn:
        return repo.ratings_for_query(conn, client.query_ids[query_index]).get(PMID)


def test_l_encart_propose_les_trois_notes(client: TestClient) -> None:
    html = client.get(
        f"/articles/{PMID}/panel", params={"query_id": client.query_ids[0]}
    ).text
    assert "Pertinence ici" in html
    for libelle in ("1 · hors sujet", "2 · secondaire", "3 · pertinent"):
        assert libelle in html
    assert "note-choix n3 actif" not in html


def test_noter_enregistre_et_redessine(client: TestClient) -> None:
    response = _noter(client, 3)
    assert response.status_code == 200
    assert _note_en_base(client) == 3
    # Encart : la note active est marquée, et la re-cliquer la retire.
    assert "note-choix n3 actif" in response.text
    assert '"rating": 0' in response.text
    # Ligne hors bande : le badge suit.
    assert 'hx-swap-oob="true"' in response.text
    assert 'class="badge note n3"' in response.text


def test_changer_puis_retirer_la_note(client: TestClient) -> None:
    _noter(client, 3)
    _noter(client, 1)
    assert _note_en_base(client) == 1
    response = _noter(client, 0)
    assert _note_en_base(client) is None
    assert "badge note" not in response.text


def test_la_note_est_propre_a_la_requete(client: TestClient) -> None:
    _noter(client, 3, query_index=0)
    assert _note_en_base(client, query_index=1) is None
    html = client.get(f"/queries/{client.query_ids[1]}/articles").text
    assert "badge note" not in html
    html = client.get(f"/queries/{client.query_ids[0]}/articles").text
    assert 'class="badge note n3"' in html


def test_note_hors_echelle_refusee(client: TestClient) -> None:
    assert _noter(client, 4).status_code == 422
    assert _note_en_base(client) is None


def test_article_hors_requete_introuvable(client: TestClient) -> None:
    response = client.post(
        "/articles/99999999/rating",
        data={"query_id": client.query_ids[0], "rating": 2},
    )
    assert response.status_code == 404


def test_set_rating_refuse_hors_echelle(client: TestClient) -> None:
    with get_conn(client.db_path) as conn, pytest.raises(ValueError):
        repo.set_rating(conn, client.query_ids[0], PMID, 0)


# --- Drapeau à 0 : comportement d'avant la fonctionnalité ----------------


def test_sans_pertinence_rien_n_apparait(client_sans_pertinence: TestClient) -> None:
    client = client_sans_pertinence
    liste = client.get(f"/queries/{client.query_ids[0]}/articles")
    assert liste.status_code == 200
    assert "Early vasopressors" in liste.text
    encart = client.get(
        f"/articles/{PMID}/panel", params={"query_id": client.query_ids[0]}
    )
    assert encart.status_code == 200
    assert "Pertinence ici" not in encart.text
    assert "note-choix" not in encart.text


def test_sans_pertinence_noter_est_refuse(client_sans_pertinence: TestClient) -> None:
    assert _noter(client_sans_pertinence, 3).status_code == 404
