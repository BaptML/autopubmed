"""Tests de la migration : colonnes de visite, index de recherche, sauvegarde."""

from pathlib import Path

import pytest

from app import repo
from app.db import get_conn, init_db
from app.models import ArticleSummary


def _summary(pmid: str, **kwargs) -> ArticleSummary:
    champs = dict(
        pmid=pmid,
        title=f"Titre {pmid}",
        journal="N Engl J Med",
        issn="00284793",
        eissn=None,
        pub_date="2026-01-15",
    )
    champs.update(kwargs)
    return ArticleSummary(**champs)


def _rabaisser_au_schema_precedent(path: Path) -> None:
    """Ramène une base à l'état d'avant la migration, pour rejouer celle-ci."""
    with get_conn(path) as conn:
        conn.executescript(
            "DROP TRIGGER IF EXISTS articles_fts_insert;"
            "DROP TRIGGER IF EXISTS articles_fts_delete;"
            "DROP TRIGGER IF EXISTS articles_fts_update;"
            "DROP TABLE IF EXISTS articles_fts;"
            "ALTER TABLE queries DROP COLUMN last_visited_at;"
            "ALTER TABLE queries DROP COLUMN previous_visit_at;"
        )


def _colonnes(conn, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _pmids_trouves(conn, terme: str) -> list[str]:
    # Pas d'alias sur la table FTS : `MATCH` exige son nom réel à gauche.
    rows = conn.execute(
        "SELECT a.pmid FROM articles_fts JOIN articles a ON a.rowid = articles_fts.rowid "
        "WHERE articles_fts MATCH ? ORDER BY a.pmid",
        (terme,),
    ).fetchall()
    return [row["pmid"] for row in rows]


@pytest.fixture
def base_peuplee(tmp_path: Path) -> Path:
    """Base à jour, avec une requête et deux articles."""
    path = tmp_path / "veille.db"
    init_db(path)
    with get_conn(path) as conn:
        query_id = repo.create_query(conn, name="A", pubmed_query="x")
        repo.upsert_article(conn, _summary("1", title="Étude pédiatrique du sommeil"))
        repo.upsert_article(conn, _summary("2", title="Randomized controlled trial"))
        repo.link_article(conn, query_id, "1")
        repo.link_article(conn, query_id, "2")
    return path


# --- Base neuve ----------------------------------------------------------


def test_base_neuve_porte_les_colonnes_de_visite(tmp_path: Path) -> None:
    path = init_db(tmp_path / "neuve.db")
    with get_conn(path) as conn:
        assert {"last_visited_at", "previous_visit_at"} <= _colonnes(conn, "queries")


def test_base_neuve_laisse_le_repere_vide(tmp_path: Path) -> None:
    """NULL, et non `now` : le repère d'une requête neuve sera son created_at."""
    path = init_db(tmp_path / "neuve.db")
    with get_conn(path) as conn:
        repo.create_query(conn, name="A", pubmed_query="x")
        row = conn.execute("SELECT previous_visit_at FROM queries").fetchone()
        assert row["previous_visit_at"] is None


def test_base_neuve_ne_prend_pas_de_sauvegarde(tmp_path: Path) -> None:
    init_db(tmp_path / "neuve.db")
    assert not (tmp_path / "backups").exists()


# --- Migration d'une base existante --------------------------------------


def test_migration_ajoute_les_colonnes(base_peuplee: Path) -> None:
    _rabaisser_au_schema_precedent(base_peuplee)
    init_db(base_peuplee)
    with get_conn(base_peuplee) as conn:
        assert {"last_visited_at", "previous_visit_at"} <= _colonnes(conn, "queries")


def test_migration_cale_les_requetes_existantes(base_peuplee: Path) -> None:
    """Sinon leur repère tomberait sur created_at et tout paraîtrait nouveau."""
    _rabaisser_au_schema_precedent(base_peuplee)
    init_db(base_peuplee)
    with get_conn(base_peuplee) as conn:
        row = conn.execute(
            "SELECT created_at, previous_visit_at FROM queries"
        ).fetchone()
        assert row["previous_visit_at"] is not None
        assert row["previous_visit_at"] >= row["created_at"]


def test_migration_prend_une_sauvegarde(base_peuplee: Path) -> None:
    _rabaisser_au_schema_precedent(base_peuplee)
    init_db(base_peuplee)
    sauvegardes = list((base_peuplee.parent / "backups").glob("*.db"))
    assert len(sauvegardes) == 1
    assert sauvegardes[0].stat().st_size > 0


def test_la_sauvegarde_contient_les_donnees(base_peuplee: Path) -> None:
    _rabaisser_au_schema_precedent(base_peuplee)
    init_db(base_peuplee)
    sauvegarde = next((base_peuplee.parent / "backups").glob("*.db"))
    with get_conn(sauvegarde) as conn:
        assert conn.execute("SELECT count(*) AS n FROM articles").fetchone()["n"] == 2


def test_migration_reconstruit_l_index_avec_l_existant(base_peuplee: Path) -> None:
    """L'index est bâti par 'rebuild' : les articles d'avant doivent s'y trouver."""
    _rabaisser_au_schema_precedent(base_peuplee)
    init_db(base_peuplee)
    with get_conn(base_peuplee) as conn:
        assert _pmids_trouves(conn, "sommeil") == ["1"]


def test_init_db_est_idempotent(base_peuplee: Path) -> None:
    """Rejouer la migration ne recale pas les repères ni ne reprend de sauvegarde."""
    _rabaisser_au_schema_precedent(base_peuplee)
    init_db(base_peuplee)
    with get_conn(base_peuplee) as conn:
        avant = conn.execute("SELECT previous_visit_at FROM queries").fetchone()[0]

    init_db(base_peuplee)
    with get_conn(base_peuplee) as conn:
        apres = conn.execute("SELECT previous_visit_at FROM queries").fetchone()[0]
    assert apres == avant
    assert len(list((base_peuplee.parent / "backups").glob("*.db"))) == 1


# --- Purge des publications futures --------------------------------------


def test_migration_purge_les_articles_dates_dans_le_futur(base_peuplee: Path) -> None:
    """Nettoyage unique : le favori garde sa ligne, mais plus de rattachement."""
    with get_conn(base_peuplee) as conn:
        query_id = conn.execute("SELECT id FROM queries").fetchone()["id"]
        repo.upsert_article(conn, _summary("f1", pub_date="2999-01-01"))
        repo.upsert_article(conn, _summary("f2", pub_date="2999-01-01"))
        repo.link_article(conn, query_id, "f1")
        repo.link_article(conn, query_id, "f2")
        repo.set_favorite(conn, "f2", True)
        conn.execute("DELETE FROM app_state WHERE key = 'future_articles_purged'")

    init_db(base_peuplee)

    with get_conn(base_peuplee) as conn:
        pmids = {r["pmid"] for r in conn.execute("SELECT pmid FROM articles")}
        lies = {r["pmid"] for r in conn.execute("SELECT pmid FROM query_articles")}
    assert pmids == {"1", "2", "f2"}  # f1 (non favori) quitte le cache
    assert lies == {"1", "2"}  # f2 (favori) reste en biblio mais sans rattachement
    assert len(list((base_peuplee.parent / "backups").glob("*.db"))) == 1


def test_la_purge_ne_se_rejoue_pas(base_peuplee: Path) -> None:
    """Le drapeau posé, un redémarrage ne reprend ni sauvegarde ni suppression."""
    with get_conn(base_peuplee) as conn:
        query_id = conn.execute("SELECT id FROM queries").fetchone()["id"]
        repo.upsert_article(conn, _summary("tardif", pub_date="2999-01-01"))
        repo.link_article(conn, query_id, "tardif")

    init_db(base_peuplee)  # drapeau déjà posé par la fixture : rien ne bouge

    with get_conn(base_peuplee) as conn:
        pmids = {r["pmid"] for r in conn.execute("SELECT pmid FROM articles")}
    assert "tardif" in pmids
    assert not (base_peuplee.parent / "backups").exists()


# --- Index de recherche ---------------------------------------------------


def test_l_index_ignore_les_accents(base_peuplee: Path) -> None:
    with get_conn(base_peuplee) as conn:
        assert _pmids_trouves(conn, "pediatrique") == ["1"]


def test_le_trigger_suit_les_insertions(base_peuplee: Path) -> None:
    with get_conn(base_peuplee) as conn:
        repo.upsert_article(conn, _summary("3", title="Cardiologie interventionnelle"))
        assert _pmids_trouves(conn, "cardiologie") == ["3"]


def test_le_trigger_suit_les_mises_a_jour(base_peuplee: Path) -> None:
    """Le résumé arrive après coup : l'index doit s'enrichir au fil des lectures."""
    with get_conn(base_peuplee) as conn:
        assert _pmids_trouves(conn, "melatonine") == []
        conn.execute(
            "UPDATE articles SET abstract = ? WHERE pmid = ?",
            ("Essai sur la mélatonine.", "1"),
        )
        assert _pmids_trouves(conn, "melatonine") == ["1"]


def test_le_trigger_suit_les_suppressions(base_peuplee: Path) -> None:
    with get_conn(base_peuplee) as conn:
        conn.execute("DELETE FROM articles WHERE pmid = ?", ("1",))
        assert _pmids_trouves(conn, "sommeil") == []


def test_l_index_reste_integre(base_peuplee: Path) -> None:
    """La vérification d'intégrité de FTS5 : lève si l'index a divergé."""
    with get_conn(base_peuplee) as conn:
        conn.execute("INSERT INTO articles_fts(articles_fts) VALUES('integrity-check')")
