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


# --- Prédiction ---------------------------------------------------------------

# Huit articles à noter : six sur le choc septique (3), deux sur l'asthme (1).
A_NOTER = {
    f"5000000{i}": (titre, note)
    for i, (titre, note) in enumerate(
        [
            ("Norepinephrine in septic shock", 3),
            ("Vasopressin for septic shock", 3),
            ("Septic shock fluid resuscitation", 3),
            ("Hydrocortisone in septic shock", 3),
            ("Septic shock mortality trends", 3),
            ("Angiotensin for septic shock", 3),
            ("Pediatric asthma inhalers", 1),
            ("Asthma in adults", 1),
        ]
    )
}


def _ajouter_et_noter(client: TestClient, combien: int) -> None:
    query_id = client.query_ids[0]
    with get_conn(client.db_path) as conn:
        for pmid, (titre, _) in A_NOTER.items():
            repo.upsert_article(
                conn,
                ArticleSummary(
                    pmid=pmid,
                    title=titre,
                    journal="Crit Care",
                    issn=None,
                    eissn=None,
                    pub_date="2026-08-01",
                ),
            )
            repo.link_article(conn, query_id, pmid)
    for pmid, (_, note) in list(A_NOTER.items())[:combien]:
        response = client.post(
            f"/articles/{pmid}/rating", data={"query_id": query_id, "rating": note}
        )
        assert response.status_code == 200


def test_jauge_d_apprentissage(client: TestClient) -> None:
    _ajouter_et_noter(client, 3)
    html = client.get(f"/queries/{client.query_ids[0]}/articles").text
    assert "Apprentissage · 3/8 notes" in html
    assert "badge prediction" not in html


def test_noter_le_huitieme_active_la_prediction(client: TestClient) -> None:
    _ajouter_et_noter(client, 8)
    html = client.get(f"/queries/{client.query_ids[0]}/articles").text
    assert "Pertinence apprise · 8 notes" in html
    # L'article d'origine, non noté, reçoit une prédiction.
    assert "badge prediction" in html

    encart = client.get(
        f"/articles/{PMID}/panel", params={"query_id": client.query_ids[0]}
    ).text
    assert "Prédiction ≈" in encart
    assert "(noté 3)" in encart


def test_la_prediction_ne_fuit_pas_dans_une_autre_requete(client: TestClient) -> None:
    _ajouter_et_noter(client, 8)
    html = client.get(f"/queries/{client.query_ids[1]}/articles").text
    assert "badge prediction" not in html
    assert "Apprentissage · 0/8 notes" in html


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


# --- Tri et masquage ------------------------------------------------------


def _enregistrer(client: TestClient, **champs) -> object:
    """Soumet le formulaire d'édition de la première requête."""
    data = {"name": "Sepsis", "pubmed_query": "Sepsis", "folder": "Sans dossier"}
    data.update(champs)
    return client.post(f"/queries/{client.query_ids[0]}/edit", data=data)


def _seuil(client: TestClient) -> float | None:
    with get_conn(client.db_path) as conn:
        return repo.get_hide_below(conn, client.query_ids[0])


def test_le_masquage_attend_que_le_modele_soit_pret(client: TestClient) -> None:
    _ajouter_et_noter(client, 3)
    html = client.get(f"/queries/{client.query_ids[0]}/edit").text
    assert '<option value="relevance"' in html
    assert 'name="hide_below"\n                  disabled' in html
    assert "(3/8 notes)" in html

    _ajouter_et_noter(client, 8)
    html = client.get(f"/queries/{client.query_ids[0]}/edit").text
    assert 'name="hide_below"\n                  disabled' not in html
    assert "Réversible" in html


def test_masquer_depuis_le_formulaire_puis_afficher(client: TestClient) -> None:
    _ajouter_et_noter(client, 8)
    response = _enregistrer(client, sort="relevance", hide_below="2")
    assert response.status_code == 200
    assert _seuil(client) == 2.0
    # Les deux articles notés 1 sont masqués, et le compteur le dit.
    assert "2 masqués · afficher" in response.text
    assert "Asthma in adults" not in response.text
    assert "masqué sous 2,0 de pertinence" in response.text

    tous = client.get(f"/queries/{client.query_ids[0]}/articles?show_hidden=1").text
    assert "2 masqués · masquer" in tous
    assert "Asthma in adults" in tous
    assert 'class="article non-lu masque"' in tous
    # Rafraîchir ou tout marquer lu garde la liste dans le même état.
    assert '"show_hidden": 1' in tous


def test_ne_rien_masquer_rend_tout(client: TestClient) -> None:
    _ajouter_et_noter(client, 8)
    _enregistrer(client, hide_below="2.5")
    response = _enregistrer(client, hide_below="aucun")
    assert _seuil(client) is None
    assert "masqués" not in response.text
    assert "Asthma in adults" in response.text


def test_un_formulaire_sans_le_champ_garde_le_seuil(client: TestClient) -> None:
    _ajouter_et_noter(client, 8)
    _enregistrer(client, hide_below="2")
    _enregistrer(client)  # menu désactivé : le champ n'est pas envoyé
    assert _seuil(client) == 2.0


def test_seuil_invalide_refuse(client: TestClient) -> None:
    assert _enregistrer(client, hide_below="7").status_code == 422
    assert _enregistrer(client, hide_below="abc").status_code == 422
    assert _seuil(client) is None


def test_sans_pertinence_ni_tri_ni_masquage(client_sans_pertinence: TestClient) -> None:
    client = client_sans_pertinence
    html = client.get(f"/queries/{client.query_ids[0]}/edit").text
    assert "hide_below" not in html
    assert '<option value="relevance"' not in html
    # Un tri « relevance » resté en base retombe sur l'ordre chronologique.
    response = _enregistrer(client, sort="relevance", hide_below="2")
    assert response.status_code == 200
    assert "Early vasopressors" in response.text
    assert "masqués" not in response.text


# --- Suggestions pour la chaîne -------------------------------------------


def _chaine(client: TestClient) -> str:
    with get_conn(client.db_path) as conn:
        return repo.get_query(conn, client.query_ids[0]).pubmed_query


def test_le_formulaire_propose_des_fragments_a_copier(client: TestClient) -> None:
    _ajouter_et_noter(client, 8)
    avant = _chaine(client)
    html = client.get(f"/queries/{client.query_ids[0]}/edit").text
    assert "Affiner la chaîne d'après tes notes" in html
    assert '<code class="a-copier">AND &#34;septic shock&#34;[tiab]</code>' in html
    assert '<code class="a-copier">NOT &#34;asthma&#34;[tiab]</code>' in html
    assert "6/6 pertinents · 0/2 hors sujet" in html
    # Afficher n'écrit rien : la chaîne enregistrée et le champ restent intacts.
    assert _chaine(client) == avant
    assert ">Sepsis</textarea>" in html


def test_suggestions_vides_avant_les_deux_groupes(client: TestClient) -> None:
    _ajouter_et_noter(client, 6)  # que des 3
    html = client.get(f"/queries/{client.query_ids[0]}/edit").text
    assert "Pas encore de terme distinctif" in html
    assert "[tiab]" not in html


def test_pas_de_suggestions_sur_une_requete_neuve(client: TestClient) -> None:
    assert "Affiner la chaîne" not in client.get("/queries/new").text


def test_sans_pertinence_pas_de_suggestions(
    client_sans_pertinence: TestClient,
) -> None:
    client = client_sans_pertinence
    html = client.get(f"/queries/{client.query_ids[0]}/edit").text
    assert "Affiner la chaîne" not in html
