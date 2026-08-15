"""Tests de l'import Scimago : parseurs purs, puis import sur base temporaire."""

from pathlib import Path

import pytest

from app.db import get_conn, init_db
from app.scimago_import import import_csv, parse_decimal, parse_quartile

# Extrait fidèle du CSV 2025 : point-virgule, virgule décimale, deux ISSN
# collés, et une revue non classée ('-').
CSV_SAMPLE = (
    "Rank;Sourceid;Title;Type;Issn;SJR;SJR Best Quartile;H index\n"
    "23;12345;New England Journal of Medicine;journal;"
    '"00284793, 15334406";18,397;Q1;1200\n'
    "999;67890;Journal Obscur;journal;12345679;0,101;Q4;12\n"
    "1000;67891;Revue Non Classee;journal;98765432;;-;3\n"
    "1001;67892;Sans Issn;journal;-;0,5;Q3;1\n"
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Q1", 1), ("Q2", 2), ("Q3", 3), ("Q4", 4), ("q1", 1), (" Q1 ", 1)],
)
def test_parse_quartile_valide(raw: str, expected: int) -> None:
    assert parse_quartile(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "-", "Q5", "Q0", "Q", "n/a"])
def test_parse_quartile_non_classee(raw: str | None) -> None:
    assert parse_quartile(raw) is None


def test_parse_decimal_virgule_decimale() -> None:
    # Le piège du format Scimago : float("18,397") lèverait ValueError.
    assert parse_decimal("18,397") == pytest.approx(18.397)
    assert parse_decimal("0,101") == pytest.approx(0.101)


@pytest.mark.parametrize("raw", [None, "", "-", "abc"])
def test_parse_decimal_absent(raw: str | None) -> None:
    assert parse_decimal(raw) is None


@pytest.fixture
def csv_file(tmp_path: Path) -> Path:
    path = tmp_path / "scimago_extrait.csv"
    path.write_text(CSV_SAMPLE, encoding="utf-8-sig")
    return path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    init_db(path)
    return path


def test_import_indexe_chaque_issn_de_la_revue(csv_file: Path, db_path: Path) -> None:
    with get_conn(db_path) as conn:
        report = import_csv(csv_file, conn)

    assert report.rows_read == 4
    assert report.rows_without_issn == 1  # « Sans Issn »
    assert report.unranked_journals == 1  # « Revue Non Classee »
    assert report.issn_rows_written == 4  # NEJM compte pour 2

    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT issn, journal_name, quartile, sjr FROM journal_quality "
            "WHERE issn IN ('00284793', '15334406') ORDER BY issn"
        ).fetchall()

    # Les deux ISSN du NEJM mènent au même verdict qualité : c'est ce qui rend
    # la jointure robuste, quel que soit celui que PubMed renvoie.
    assert len(rows) == 2
    for row in rows:
        assert row["journal_name"] == "New England Journal of Medicine"
        assert row["quartile"] == 1
        assert row["sjr"] == pytest.approx(18.397)


def test_import_revue_non_classee_a_un_quartile_null(
    csv_file: Path, db_path: Path
) -> None:
    with get_conn(db_path) as conn:
        import_csv(csv_file, conn)
        row = conn.execute(
            "SELECT quartile, sjr FROM journal_quality WHERE issn = '98765432'"
        ).fetchone()

    assert row["quartile"] is None
    assert row["sjr"] is None


def test_import_est_idempotent(csv_file: Path, db_path: Path) -> None:
    with get_conn(db_path) as conn:
        import_csv(csv_file, conn)
        first = conn.execute("SELECT COUNT(*) AS n FROM journal_quality").fetchone()["n"]
    with get_conn(db_path) as conn:
        import_csv(csv_file, conn)
        second = conn.execute("SELECT COUNT(*) AS n FROM journal_quality").fetchone()["n"]

    assert first == second == 4


def test_reimport_scimago_preserve_la_citedness_openalex(
    csv_file: Path, db_path: Path
) -> None:
    # OpenAlex alimente `citedness` ; le réimport annuel de Scimago ne doit
    # pas l'effacer en repassant sur la même ligne.
    with get_conn(db_path) as conn:
        import_csv(csv_file, conn)
        conn.execute(
            "UPDATE journal_quality SET citedness = 74.2 WHERE issn = '00284793'"
        )
    with get_conn(db_path) as conn:
        import_csv(csv_file, conn)
        row = conn.execute(
            "SELECT citedness, quartile FROM journal_quality WHERE issn = '00284793'"
        ).fetchone()

    assert row["citedness"] == pytest.approx(74.2)
    assert row["quartile"] == 1
