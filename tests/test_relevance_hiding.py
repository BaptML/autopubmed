"""Tri par pertinence et masquage réversible : liste, compteurs, flux, références."""

import asyncio
import dataclasses
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app import db, repo
from app.db import get_conn, init_db
from app.models import ArticleSummary, ESearchResult
from app.refresh import refresh_query
from app.relevance import recompute_scores

NEJM_ISSN = "00284793"

# Huit notes (six 3 sur le choc septique, deux 1 sur l'asthme) et quatre
# articles à prédire : deux proches du choc septique, deux de l'asthme.
NOTES = {
    "n1": ("Norepinephrine in septic shock", 3),
    "n2": ("Vasopressin for septic shock", 3),
    "n3": ("Septic shock fluid resuscitation", 3),
    "n4": ("Hydrocortisone in septic shock", 3),
    "n5": ("Septic shock mortality trends", 3),
    "n6": ("Angiotensin for septic shock", 3),
    "n7": ("Pediatric asthma inhalers", 1),
    "n8": ("Asthma in adults", 1),
}
A_PREDIRE = {
    "x_choc1": "Early vasopressors in septic shock",
    "x_choc2": "Septic shock and steroids",
    "x_asthme1": "Asthma inhalers in adults",
    "x_asthme2": "Pediatric asthma in adults",
}


def _il_y_a(minutes: int) -> str:
    moment = datetime.now(UTC) - timedelta(minutes=minutes)
    return moment.replace(microsecond=0).isoformat()


def _ajouter(conn: sqlite3.Connection, query_id: int, pmid: str, titre: str, jour: int):
    repo.upsert_article(
        conn,
        ArticleSummary(
            pmid=pmid,
            title=titre,
            journal="N Engl J Med",
            issn=NEJM_ISSN,
            eissn=None,
            pub_date=f"2026-01-{jour:02d}",
        ),
    )
    repo.link_article(conn, query_id, pmid)


@pytest.fixture
def bac(tmp_path: Path):
    """Une requête de douze articles, visitée il y a une heure, sans aucune note."""
    path = tmp_path / "masquage.db"
    init_db(path)
    with get_conn(path) as conn:
        query_id = repo.create_query(conn, name="Sepsis", pubmed_query="sepsis")
        titres = {p: t for p, (t, _) in NOTES.items()} | A_PREDIRE
        for jour, (pmid, titre) in enumerate(titres.items(), start=1):
            _ajouter(conn, query_id, pmid, titre, jour)
        conn.execute("UPDATE query_articles SET first_seen = ?", (_il_y_a(minutes=10),))
        conn.execute(
            "UPDATE queries SET last_visited_at = ?, previous_visit_at = ?",
            (_il_y_a(minutes=60), _il_y_a(minutes=60)),
        )
        yield conn, query_id


def _noter(conn: sqlite3.Connection, query_id: int) -> None:
    for pmid, (_, note) in NOTES.items():
        repo.set_rating(conn, query_id, pmid, note)
    recompute_scores(conn, query_id)


def _pmids(conn: sqlite3.Connection, query_id: int, **kwargs) -> list[str]:
    return [a["pmid"] for a in repo.list_articles_for_query(conn, query_id, **kwargs)]


# --- Tri --------------------------------------------------------------------


def test_sans_notes_le_tri_pertinence_est_le_tri_par_date(bac) -> None:
    conn, query_id = bac
    assert _pmids(conn, query_id, sort="relevance") == _pmids(
        conn, query_id, sort="date_desc"
    )


def test_le_tri_pertinence_suit_la_note_puis_la_prediction(bac) -> None:
    conn, query_id = bac
    _noter(conn, query_id)
    articles = repo.list_articles_for_query(conn, query_id, sort="relevance")
    valeurs = [
        a["rating"] if a["rating"] is not None else a["predicted"] for a in articles
    ]
    assert valeurs == sorted(valeurs, reverse=True)
    # Les notes 3 passent devant les prédictions, si hautes soient-elles.
    assert {a["pmid"] for a in articles[:6]} == {f"n{i}" for i in range(1, 7)}
    assert articles[-1]["rating"] == 1


def test_sans_note_ni_prediction_l_article_finit_par_date(bac) -> None:
    conn, query_id = bac
    repo.set_rating(conn, query_id, "x_asthme2", 1)  # une seule note : pas de modèle
    pmids = _pmids(conn, query_id, sort="relevance")
    assert pmids[0] == "x_asthme2"
    reste = _pmids(conn, query_id, sort="date_desc")
    reste.remove("x_asthme2")
    assert pmids[1:] == reste


# --- Masquage ---------------------------------------------------------------


def test_masquer_puis_desactiver_rend_la_liste_identique(bac) -> None:
    conn, query_id = bac
    _noter(conn, query_id)
    avant = repo.list_articles_for_query(conn, query_id, sort="relevance")

    repo.set_hide_below(conn, query_id, 2.0)
    visibles = _pmids(conn, query_id, sort="relevance")
    assert {"n7", "n8", "x_asthme1", "x_asthme2"}.isdisjoint(visibles)
    assert {"x_choc1", "x_choc2"} <= set(visibles)
    assert repo.count_hidden_for_query(conn, query_id) == len(avant) - len(visibles)

    # Les masqués restent lisibles sur demande, marqués comme tels.
    tous = repo.list_articles_for_query(
        conn, query_id, sort="relevance", show_hidden=True
    )
    assert [a["pmid"] for a in tous] == [a["pmid"] for a in avant]
    masques = {a["pmid"] for a in avant} - set(visibles)
    assert {a["pmid"] for a in tous if a["is_hidden"]} == masques

    repo.set_hide_below(conn, query_id, None)
    assert repo.list_articles_for_query(conn, query_id, sort="relevance") == avant
    assert repo.count_hidden_for_query(conn, query_id) == 0


def test_le_comptage_suit_la_liste(bac) -> None:
    conn, query_id = bac
    _noter(conn, query_id)
    repo.set_hide_below(conn, query_id, 2.5)
    for show_hidden in (False, True):
        assert repo.count_articles_for_query(
            conn, query_id, show_hidden=show_hidden
        ) == len(_pmids(conn, query_id, show_hidden=show_hidden))


def test_les_non_lus_et_le_flux_ne_contredisent_pas_la_liste(bac) -> None:
    conn, query_id = bac
    _noter(conn, query_id)
    repo.set_hide_below(conn, query_id, 2.0)
    visibles = _pmids(conn, query_id)

    assert repo.count_unread(conn, query_id) == len(visibles)
    barre = repo.folders_with_queries(conn)[0]["queries"][0]
    assert barre["unread"] == len(visibles)
    assert barre["total"] == len(NOTES) + len(A_PREDIRE)  # le total reste brut

    flux = [a["pmid"] for a in repo.list_new_articles(conn)]
    assert sorted(flux) == sorted(visibles)
    assert repo.count_new_articles(conn) == len(visibles)


def test_tout_marquer_lu_epargne_les_masques(bac) -> None:
    conn, query_id = bac
    _noter(conn, query_id)
    repo.set_hide_below(conn, query_id, 2.0)
    masques = repo.count_hidden_for_query(conn, query_id)
    repo.mark_all_read(conn, query_id)
    assert repo.count_unread(conn, query_id) == 0
    # Réaffichés, ils sont toujours à lire.
    assert repo.count_unread(conn, query_id, show_hidden=True) == masques


def test_un_article_sans_score_n_est_jamais_masque(bac) -> None:
    conn, query_id = bac
    avant = _pmids(conn, query_id)
    repo.set_hide_below(conn, query_id, 2.5)  # aucune note : rien à comparer
    assert _pmids(conn, query_id) == avant
    assert repo.count_hidden_for_query(conn, query_id) == 0


def test_le_seuil_est_propre_a_la_requete(bac) -> None:
    conn, query_id = bac
    autre = repo.create_query(conn, name="Autre", pubmed_query="autre")
    for pmid in NOTES:
        repo.link_article(conn, autre, pmid)
    _noter(conn, query_id)
    repo.set_hide_below(conn, query_id, 2.0)
    assert len(_pmids(conn, autre)) == len(NOTES)
    assert repo.get_hide_below(conn, autre) is None


def test_seuil_hors_echelle_refuse(bac) -> None:
    conn, query_id = bac
    for seuil in (0.5, 1.0, 3.5):
        with pytest.raises(ValueError):
            repo.set_hide_below(conn, query_id, seuil)


def test_drapeau_a_zero_neutralise_un_seuil_pose(
    bac, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, query_id = bac
    _noter(conn, query_id)
    repo.set_hide_below(conn, query_id, 2.5)
    monkeypatch.setattr(
        db, "settings", dataclasses.replace(db.settings, enable_relevance=False)
    )
    assert len(_pmids(conn, query_id, sort="relevance")) == len(NOTES) + len(A_PREDIRE)
    assert repo.count_hidden_for_query(conn, query_id) == 0
    assert repo.get_hide_below(conn, query_id) is None


# --- Requêtes de référence ----------------------------------------------------


class _PubMed:
    """Client simulé : renvoie toujours le même lot d'articles."""

    def __init__(self, articles: list[ArticleSummary]) -> None:
        self.articles = articles

    async def esearch(self, term: str, *, reldate_days: int | None = None):
        pmids = [a.pmid for a in self.articles]
        return ESearchResult(pmids, len(pmids), term, [], [])

    async def esummary(self, pmids: list[str]) -> list[ArticleSummary]:
        return [a for a in self.articles if a.pmid in pmids]


def _lot(revues: list[tuple[str, str]]) -> list[ArticleSummary]:
    """Deux articles par revue : un sur le choc septique, un sur l'asthme."""
    lot = []
    for i, (revue, issn) in enumerate(revues):
        for j, titre in enumerate(("Vasopressors in septic shock", "Asthma inhalers")):
            lot.append(
                ArticleSummary(
                    pmid=f"{i}{j}",
                    title=f"{titre} {i}",
                    journal=revue,
                    issn=issn,
                    eissn=None,
                    pub_date="2026-01-15",
                )
            )
    return lot


@pytest.fixture
def base_reference(tmp_path: Path) -> Path:
    path = tmp_path / "reference.db"
    init_db(path)
    with get_conn(path) as conn:
        conn.executemany(
            "INSERT INTO journal_quality (issn, journal_name, quartile, source, "
            "updated_at) VALUES (?, ?, ?, 'scimago', '2026-01-01T00:00:00+00:00')",
            [
                (NEJM_ISSN, "New England Journal of Medicine", 1),
                ("11110000", "Crit Care", 1),
                ("22220000", "Rev Moyenne", 3),
            ],
        )
    return path


def _noter_et_masquer(conn: sqlite3.Connection, query_id: int) -> None:
    """Note tous les articles retenus (choc septique 3, asthme 1), puis masque."""
    for article in repo.list_articles_for_query(conn, query_id):
        note = 3 if "septic" in article["title"] else 1
        repo.set_rating(conn, query_id, article["pmid"], note)
    recompute_scores(conn, query_id)
    repo.set_hide_below(conn, query_id, 1.5)


def _rafraichir(conn: sqlite3.Connection, query_id: int, lot) -> None:
    query = repo.get_query(conn, query_id)
    assert query is not None
    asyncio.run(refresh_query(conn, query, pubmed=_PubMed(lot)))  # type: ignore[arg-type]


def test_reference_a_la_pertinence_ne_readmet_rien(base_reference: Path) -> None:
    """A (Q1) : notés, masqués puis réaffichés, toujours aucune revue hors Q1."""
    lot = _lot(
        [(f"Crit Care {i}", "11110000") for i in range(5)]
        + [("Rev Moyenne", "22220000"), ("Rev Inconnue", "33330000")]
    )
    with get_conn(base_reference) as conn:
        query_id = repo.create_query(
            conn, name="A", pubmed_query="sepsis[tiab]", quartile_min=1
        )
        _rafraichir(conn, query_id, lot)
        retenus = _pmids(conn, query_id)
        _noter_et_masquer(conn, query_id)
        _rafraichir(conn, query_id, lot)  # étape 7 : recalcul, aucun rattachement

        for show_hidden in (False, True):
            articles = repo.list_articles_for_query(
                conn, query_id, show_hidden=show_hidden
            )
            assert articles
            assert {a["quartile"] for a in articles} == {1}
        assert sorted(_pmids(conn, query_id, show_hidden=True)) == sorted(retenus)
        assert repo.count_hidden_for_query(conn, query_id) == 5  # les cinq asthmes


def test_reference_b_le_total_revient_a_l_identique(base_reference: Path) -> None:
    """B (NEJM/RCT) : uniquement N Engl J Med, et masquage retiré = total d'avant."""
    lot = _lot([("N Engl J Med", NEJM_ISSN)] * 5)
    with get_conn(base_reference) as conn:
        query_id = repo.create_query(
            conn,
            name="B",
            pubmed_query='"N Engl J Med"[ta] AND randomized controlled trial[pt]',
        )
        _rafraichir(conn, query_id, lot)
        total_avant = repo.count_articles_for_query(conn, query_id)
        _noter_et_masquer(conn, query_id)
        # Un tri par pertinence ne réécrit pas la chaîne PubMed.
        query = repo.get_query(conn, query_id)
        assert query is not None
        assert query.pubmed_query.startswith('"N Engl J Med"[ta]')

        assert repo.count_articles_for_query(conn, query_id) < total_avant
        journaux = {
            a["journal"]
            for a in repo.list_articles_for_query(conn, query_id, show_hidden=True)
        }
        assert journaux == {"N Engl J Med"}

        repo.set_hide_below(conn, query_id, None)
        assert repo.count_articles_for_query(conn, query_id) == total_avant
