"""Couche pertinence : migration additive, désinstallation, réimport."""

import dataclasses
import json
from pathlib import Path

import pytest

from app import db, repo
from app.db import RELEVANCE_TABLES, get_conn, init_db
from app.models import ArticleSummary
from app.relevance_admin import import_ratings, main, uninstall


def _summary(pmid: str) -> ArticleSummary:
    return ArticleSummary(
        pmid=pmid,
        title=f"Titre {pmid}",
        journal="N Engl J Med",
        issn="00284793",
        eissn=None,
        pub_date="2026-01-15",
    )


@pytest.fixture
def sans_pertinence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drapeau ENABLE_RELEVANCE à 0, le temps d'un test."""
    monkeypatch.setattr(
        db, "settings", dataclasses.replace(db.settings, enable_relevance=False)
    )


def _tables(path: Path) -> set[str]:
    with get_conn(path) as conn:
        return {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def _schema_existant(path: Path) -> dict[str, str]:
    """SQL de tout ce qui n'appartient pas à la couche pertinence."""
    with get_conn(path) as conn:
        return {
            row["name"]: row["sql"]
            for row in conn.execute("SELECT name, sql FROM sqlite_master")
            if row["name"] not in RELEVANCE_TABLES
            and not row["name"].startswith("sqlite_autoindex_")
        }


def _sauvegardes(path: Path) -> list[Path]:
    return list((path.parent / "backups").glob("*.db"))


@pytest.fixture
def base_avant_fonction(tmp_path: Path, sans_pertinence: None) -> Path:
    """Base peuplée telle que la créait le code d'avant la fonctionnalité."""
    path = tmp_path / "veille.db"
    init_db(path)
    with get_conn(path) as conn:
        query_id = repo.create_query(conn, name="A", pubmed_query="x")
        for pmid in ("1", "2"):
            repo.upsert_article(conn, _summary(pmid))
            repo.link_article(conn, query_id, pmid)
    return path


def _activer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        db, "settings", dataclasses.replace(db.settings, enable_relevance=True)
    )


# --- Base neuve ------------------------------------------------------------


def test_base_neuve_porte_les_tables(tmp_path: Path) -> None:
    path = init_db(tmp_path / "neuve.db")
    assert set(RELEVANCE_TABLES) <= _tables(path)
    assert not (tmp_path / "backups").exists()


def test_drapeau_a_zero_ne_cree_rien(tmp_path: Path, sans_pertinence: None) -> None:
    path = init_db(tmp_path / "neuve.db")
    assert not set(RELEVANCE_TABLES) & _tables(path)


# --- Migration d'une base existante ----------------------------------------


def test_migration_cree_les_tables_apres_sauvegarde(
    base_avant_fonction: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _activer(monkeypatch)
    init_db(base_avant_fonction)
    assert set(RELEVANCE_TABLES) <= _tables(base_avant_fonction)
    sauvegardes = _sauvegardes(base_avant_fonction)
    assert len(sauvegardes) == 1
    # La sauvegarde est l'état d'avant : sans les nouvelles tables.
    assert not set(RELEVANCE_TABLES) & _tables(sauvegardes[0])


def test_migration_ne_touche_aucune_table_existante(
    base_avant_fonction: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Garantie de réversibilité : l'ancien code relit la base sans surprise."""
    avant = _schema_existant(base_avant_fonction)
    _activer(monkeypatch)
    init_db(base_avant_fonction)
    assert _schema_existant(base_avant_fonction) == avant


def test_migration_idempotente(
    base_avant_fonction: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _activer(monkeypatch)
    init_db(base_avant_fonction)
    init_db(base_avant_fonction)
    assert len(_sauvegardes(base_avant_fonction)) == 1


def test_supprimer_une_requete_emporte_ses_notes(tmp_path: Path) -> None:
    path = init_db(tmp_path / "veille.db")
    with get_conn(path) as conn:
        query_id = repo.create_query(conn, name="A", pubmed_query="x")
        repo.upsert_article(conn, _summary("1"))
        conn.execute(
            "INSERT INTO article_ratings VALUES (?, '1', 3, '2026-10-25T00:00:00+00:00')",
            (query_id,),
        )
        repo.delete_query(conn, query_id)
        assert conn.execute("SELECT count(*) FROM article_ratings").fetchone()[0] == 0


def test_note_hors_echelle_refusee(tmp_path: Path) -> None:
    path = init_db(tmp_path / "veille.db")
    with get_conn(path) as conn:
        query_id = repo.create_query(conn, name="A", pubmed_query="x")
        repo.upsert_article(conn, _summary("1"))
        with pytest.raises(Exception, match="CHECK"):
            conn.execute(
                "INSERT INTO article_ratings VALUES (?, '1', 4, 'x')", (query_id,)
            )


# --- Désinstallation et réimport ------------------------------------------


@pytest.fixture
def base_notee(tmp_path: Path) -> Path:
    path = init_db(tmp_path / "veille.db")
    with get_conn(path) as conn:
        query_id = repo.create_query(conn, name="Sepsis", pubmed_query="sepsis")
        for pmid in ("1", "2"):
            repo.upsert_article(conn, _summary(pmid))
            repo.link_article(conn, query_id, pmid)
        conn.executemany(
            "INSERT INTO article_ratings VALUES (?, ?, ?, '2026-10-25T00:00:00+00:00')",
            [(query_id, "1", 3), (query_id, "2", 1)],
        )
        conn.execute("INSERT INTO relevance_settings VALUES (?, 2.0)", (query_id,))
        conn.execute(
            "INSERT INTO relevance_scores VALUES (?, '1', 2.5, 'x')", (query_id,)
        )
    return path


def test_desinstaller_sauvegarde_exporte_et_supprime(base_notee: Path) -> None:
    avant = _schema_existant(base_notee)
    report = uninstall(base_notee)

    assert report is not None and report.ratings == 2
    assert not set(RELEVANCE_TABLES) & _tables(base_notee)
    # Le reste de la base n'a pas bougé.
    assert _schema_existant(base_notee) == avant
    with get_conn(base_notee) as conn:
        assert conn.execute("SELECT count(*) FROM query_articles").fetchone()[0] == 2
    # La sauvegarde contient encore les notes.
    with get_conn(report.backup) as conn:
        assert conn.execute("SELECT count(*) FROM article_ratings").fetchone()[0] == 2
    contenu = json.loads(report.export.read_text("utf-8"))
    assert {(n["pmid"], n["rating"]) for n in contenu["ratings"]} == {
        ("1", 3),
        ("2", 1),
    }
    assert contenu["ratings"][0]["query_name"] == "Sepsis"


def test_desinstaller_sans_tables_ne_fait_rien(
    tmp_path: Path, sans_pertinence: None
) -> None:
    path = init_db(tmp_path / "veille.db")
    assert uninstall(path) is None
    assert not (tmp_path / "backups").exists()


def test_reimport_restaure_notes_et_reglages(base_notee: Path) -> None:
    report = uninstall(base_notee)
    assert report is not None

    resultat = import_ratings(base_notee, report.export)

    assert (resultat.ratings, resultat.settings, resultat.skipped) == (2, 1, 0)
    with get_conn(base_notee) as conn:
        notes = {
            (r["pmid"], r["rating"])
            for r in conn.execute("SELECT pmid, rating FROM article_ratings")
        }
        seuil = conn.execute("SELECT hide_below FROM relevance_settings").fetchone()
    assert notes == {("1", 3), ("2", 1)}
    assert seuil["hide_below"] == 2.0


def test_reimport_n_ecrase_pas_et_ignore_les_disparus(base_notee: Path) -> None:
    report = uninstall(base_notee)
    assert report is not None
    init_db(base_notee)  # tables recréées vides
    with get_conn(base_notee) as conn:
        query_id = conn.execute("SELECT id FROM queries").fetchone()["id"]
        conn.execute(
            "INSERT INTO article_ratings VALUES (?, '1', 2, 'plus récent')", (query_id,)
        )
        conn.execute("DELETE FROM articles WHERE pmid = '2'")

    resultat = import_ratings(base_notee, report.export)

    assert (resultat.ratings, resultat.skipped) == (0, 1)
    with get_conn(base_notee) as conn:
        note = conn.execute("SELECT rating FROM article_ratings").fetchone()
    assert note["rating"] == 2  # la note présente prime sur l'export


def test_cli_desinstaller(base_notee: Path, capsys: pytest.CaptureFixture) -> None:
    assert main(["--desinstaller", "--db", str(base_notee)]) == 0
    assert "Notes exportées (2)" in capsys.readouterr().out
