"""Test du pipeline refresh_query, avec un client PubMed simulé.

Aucun appel réseau : on vérifie l'enchaînement chercher → enrichir →
qualifier → filtrer → persister, et surtout que la jointure qualité aboutit
avec l'ISSN papier comme avec l'électronique.
"""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from app import repo
from app.db import get_conn, init_db
from app.models import ArticleSummary, ESearchResult
from app.refresh import refresh_query

NEJM_ISSN, NEJM_EISSN = "00284793", "15334406"


class FakePubMed:
    """Remplace PubMedClient : renvoie des réponses figées et compte les appels."""

    def __init__(self, summaries: list[ArticleSummary]) -> None:
        self._summaries = {s.pmid: s for s in summaries}
        self.esummary_calls: list[list[str]] = []

    async def esearch(self, term: str, *, reldate_days: int | None = None) -> ESearchResult:
        return ESearchResult(
            pmids=list(self._summaries),
            count=len(self._summaries),
            translation=f"{term}[traduit]",
            warnings=[],
            errors=[],
        )

    async def esummary(self, pmids: list[str]) -> list[ArticleSummary]:
        self.esummary_calls.append(pmids)
        return [self._summaries[p] for p in pmids if p in self._summaries]


def _article(pmid: str, journal: str, issn: str | None, eissn: str | None) -> ArticleSummary:
    return ArticleSummary(
        pmid=pmid, title=f"Titre {pmid}", journal=journal,
        issn=issn, eissn=eissn, pub_date="2026-01-15",
    )


ARTICLES = [
    # Repéré par son ISSN papier.
    _article("1", "N Engl J Med", NEJM_ISSN, None),
    # Repéré par son ISSN électronique seulement : cas qui échouait sans `eissn`.
    _article("2", "N Engl J Med", None, NEJM_EISSN),
    _article("3", "Rev Moyenne", "11110000", None),  # Q3
    _article("4", "Rev Non Classee", "22220000", None),  # présente mais sans quartile
    _article("5", "Rev Inconnue", "33330000", None),  # absente de journal_quality
]


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "refresh.db"
    init_db(path)
    with get_conn(path) as conn:
        conn.executemany(
            "INSERT INTO journal_quality (issn, journal_name, quartile, citedness, "
            "source, updated_at) VALUES (?, ?, ?, ?, 'scimago', '2026-01-01T00:00:00+00:00')",
            [
                (NEJM_ISSN, "New England Journal of Medicine", 1, 74.0),
                (NEJM_EISSN, "New England Journal of Medicine", 1, 74.0),
                ("11110000", "Rev Moyenne", 3, 2.0),
                ("22220000", "Rev Non Classee", None, 1.0),
            ],
        )
    return path


def _run(conn: sqlite3.Connection, query_id: int, fake: FakePubMed):
    query = repo.get_query(conn, query_id)
    assert query is not None
    return asyncio.run(refresh_query(conn, query, pubmed=fake))


def test_filtre_q1_ne_garde_que_les_revues_q1(db: Path) -> None:
    fake = FakePubMed(ARTICLES)
    with get_conn(db) as conn:
        query_id = repo.create_query(
            conn, name="A", pubmed_query="cancer[tiab]", quartile_min=1
        )
        report = _run(conn, query_id, fake)

    assert report.kept == 2  # les deux NEJM, dont un repéré par son eissn
    assert report.rejected == 3  # Q3, non classée, inconnue
    assert report.unmatched_quality == 1  # seule « Rev Inconnue » est hors table
    assert report.new_links == 2

    with get_conn(db) as conn:
        pmids = {
            row["pmid"]
            for row in conn.execute(
                "SELECT pmid FROM query_articles WHERE query_id = ?", (query_id,)
            )
        }
    assert pmids == {"1", "2"}


def test_sans_filtre_tout_est_conserve(db: Path) -> None:
    fake = FakePubMed(ARTICLES)
    with get_conn(db) as conn:
        query_id = repo.create_query(conn, name="B", pubmed_query='"N Engl J Med"[ta]')
        report = _run(conn, query_id, fake)

    assert report.kept == 5
    assert report.rejected == 0
    # La revue inconnue reste comptée comme telle, mais n'est pas écartée.
    assert report.unmatched_quality == 1


def test_second_refresh_n_invente_pas_de_nouveaux_non_lus(db: Path) -> None:
    fake = FakePubMed(ARTICLES)
    with get_conn(db) as conn:
        query_id = repo.create_query(conn, name="A", pubmed_query="x", quartile_min=1)
        first = _run(conn, query_id, fake)
        first_seen = conn.execute(
            "SELECT first_seen FROM query_articles WHERE query_id = ? AND pmid = '1'",
            (query_id,),
        ).fetchone()["first_seen"]

        second = _run(conn, query_id, fake)
        unchanged = conn.execute(
            "SELECT first_seen FROM query_articles WHERE query_id = ? AND pmid = '1'",
            (query_id,),
        ).fetchone()["first_seen"]

    assert first.new_links == 2
    assert second.new_links == 0  # rien de neuf
    assert second.kept == 2  # mais les articles restent rattachés
    assert first_seen == unchanged  # le badge « non lu » ne se réinitialise pas


def test_esummary_n_est_pas_rappele_pour_les_articles_connus(db: Path) -> None:
    fake = FakePubMed(ARTICLES)
    with get_conn(db) as conn:
        query_id = repo.create_query(conn, name="A", pubmed_query="x")
        _run(conn, query_id, fake)
        _run(conn, query_id, fake)

    assert len(fake.esummary_calls) == 1  # le second passage lit la base
    assert sorted(fake.esummary_calls[0]) == ["1", "2", "3", "4", "5"]


def test_last_refreshed_at_est_horodate(db: Path) -> None:
    fake = FakePubMed(ARTICLES)
    with get_conn(db) as conn:
        query_id = repo.create_query(conn, name="A", pubmed_query="x")
        assert repo.get_query(conn, query_id).last_refreshed_at is None
        _run(conn, query_id, fake)
        assert repo.get_query(conn, query_id).last_refreshed_at is not None


def test_recherche_vide_ne_casse_pas_le_pipeline(db: Path) -> None:
    fake = FakePubMed([])
    with get_conn(db) as conn:
        query_id = repo.create_query(conn, name="Vide", pubmed_query="zzz")
        report = _run(conn, query_id, fake)

    assert report.kept == 0 and report.new_links == 0
    with get_conn(db) as conn:
        assert repo.get_query(conn, query_id).last_refreshed_at is not None
