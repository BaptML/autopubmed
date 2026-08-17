"""Tests du job de fond : réglages du planificateur et déroulé d'un passage.

Aucun appel réseau : le client PubMed est simulé.
"""

import asyncio
import dataclasses
import logging
from pathlib import Path

import httpx
import pytest

from app import repo, scheduler
from app.config import settings
from app.db import get_conn, init_db
from app.models import ArticleSummary, ESearchResult


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "job.db"
    init_db(path)
    with get_conn(path) as conn:
        conn.execute(
            "INSERT INTO journal_quality (issn, journal_name, quartile, source, updated_at)"
            " VALUES ('00284793', 'NEJM', 1, 'scimago', '2026-01-01T00:00:00+00:00')"
        )
        repo.create_query(conn, name="NEJM", pubmed_query='"N Engl J Med"[ta]')
        repo.create_query(conn, name="Autre", pubmed_query="heart[tiab]")
    return path


class FakePubMedClient:
    """Client simulé ; peut échouer sur la première requête pour l'isolation."""

    def __init__(self, fail_first: bool = False) -> None:
        self._fail_first = fail_first
        self.esearch_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def esearch(self, term, *, reldate_days=None, retmax=None) -> ESearchResult:
        self.esearch_calls += 1
        if self._fail_first and self.esearch_calls == 1:
            raise httpx.ConnectError("réseau indisponible")
        return ESearchResult(
            pmids=["40000001"], count=1, translation=term, warnings=[], errors=[]
        )

    async def esummary(self, pmids: list[str]) -> list[ArticleSummary]:
        return [
            ArticleSummary(
                pmid=pmid,
                title=f"Titre {pmid}",
                journal="N Engl J Med",
                issn="00284793",
                eissn=None,
                pub_date="2026-08-13",
            )
            for pmid in pmids
        ]


def _settings_with(**changes) -> object:
    return dataclasses.replace(settings, **changes)


def test_pas_de_planificateur_quand_desactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler, "settings", _settings_with(enable_scheduler=False))
    assert scheduler.create_scheduler() is None


def test_planificateur_configure_selon_l_intervalle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        scheduler,
        "settings",
        _settings_with(enable_scheduler=True, scheduler_interval_minutes=45),
    )
    created = scheduler.create_scheduler()
    assert created is not None
    job = created.get_job(scheduler.JOB_ID)
    assert job.trigger.interval.total_seconds() == 45 * 60
    # Deux passages ne doivent jamais se chevaucher ni se rejouer en rafale.
    assert job.max_instances == 1
    assert job.coalesce is True


def test_un_passage_remplit_le_cache_et_les_non_lus(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.refresh.PubMedClient", FakePubMedClient)
    asyncio.run(scheduler.run_refresh(db_path))

    with get_conn(db_path) as conn:
        # Les deux requêtes ont été rafraîchies, l'article est non lu.
        assert repo.count_unread(conn, 1) == 1
        assert repo.count_unread(conn, 2) == 1
        assert repo.get_query(conn, 1).last_refreshed_at is not None
        # Le job ne récupère aucun résumé : ils restent NULL jusqu'au dépliage.
        assert repo.get_abstract(conn, "40000001") is None


def test_une_requete_en_echec_n_arrete_pas_les_autres(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        "app.refresh.PubMedClient", lambda: FakePubMedClient(fail_first=True)
    )
    with caplog.at_level(logging.WARNING, logger="app.scheduler"):
        asyncio.run(scheduler.run_refresh(db_path))

    # Les requêtes sont traitées dans l'ordre de `list_queries` (dossier puis
    # nom) : « Autre » (id 2) passe avant « NEJM » (id 1), c'est donc elle qui
    # échoue.
    with get_conn(db_path) as conn:
        assert repo.count_unread(conn, 2) == 0  # celle qui a échoué
        assert repo.count_unread(conn, 1) == 1  # la suivante est bien passée
    assert "« Autre » : réseau indisponible" in caplog.text
