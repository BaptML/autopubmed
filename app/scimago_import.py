"""Import du classement Scimago dans `journal_quality`.

Le CSV se télécharge à la main depuis
https://www.scimagojr.com/journalrank.php?out=xls (le lien est servi derrière
une protection anti-robot). Malgré l'extension annoncée, c'est un CSV séparé
par des points-virgules, encodé en UTF-8 avec BOM, et dont les nombres
utilisent la **virgule décimale**.

Usage : uv run python -m app.scimago_import data/scimagojr2025.csv
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from app.db import get_conn, init_db, utcnow_iso
from app.issn import split_issns

# Colonnes indispensables. Si l'une manque, le millésime du CSV a changé de
# format : on échoue bruyamment plutôt que d'insérer des NULL silencieux.
REQUIRED_COLUMNS = frozenset({"Title", "Issn", "SJR", "SJR Best Quartile"})


@dataclass(frozen=True, slots=True)
class ImportReport:
    rows_read: int
    rows_without_issn: int
    issn_rows_written: int
    unranked_journals: int

    def render(self) -> str:
        return (
            f"Lignes lues            : {self.rows_read}\n"
            f"Revues sans ISSN exploitable : {self.rows_without_issn}\n"
            f"Revues sans quartile   : {self.unranked_journals}\n"
            f"Entrées ISSN écrites   : {self.issn_rows_written}"
        )


def parse_quartile(raw: str | None) -> int | None:
    """« Q1 » → 1. « - », vide ou valeur inattendue → None (revue non classée)."""
    if not raw:
        return None
    candidate = raw.strip().upper()
    if len(candidate) == 2 and candidate[0] == "Q" and candidate[1] in "1234":
        return int(candidate[1])
    return None


def parse_decimal(raw: str | None) -> float | None:
    """Scimago écrit « 18,397 » : convertit la virgule décimale en point."""
    if not raw:
        return None
    candidate = raw.strip().replace(" ", "").replace(",", ".")
    if not candidate or candidate == "-":
        return None
    try:
        return float(candidate)
    except ValueError:
        return None


def iter_rows(csv_path: Path) -> Iterator[dict[str, str]]:
    """Parcourt le CSV en flux et vérifie l'en-tête avant la première ligne."""
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        columns = set(reader.fieldnames or [])
        missing = REQUIRED_COLUMNS - columns
        if missing:
            raise ValueError(
                f"Colonnes absentes du CSV Scimago : {sorted(missing)}. "
                f"En-tête lu : {sorted(columns)}"
            )
        yield from reader


_UPSERT = """
INSERT INTO journal_quality (issn, journal_name, quartile, sjr, source, updated_at)
VALUES (?, ?, ?, ?, 'scimago', ?)
ON CONFLICT(issn) DO UPDATE SET
    journal_name = excluded.journal_name,
    quartile     = excluded.quartile,
    sjr          = excluded.sjr,
    source       = 'scimago',
    updated_at   = excluded.updated_at
"""
# `citedness` est volontairement absent : il vient d'OpenAlex et ne doit pas
# être effacé par un réimport annuel de Scimago.


def import_csv(csv_path: Path, conn: sqlite3.Connection) -> ImportReport:
    """Charge le CSV dans `journal_quality`. Idempotent."""
    now = utcnow_iso()
    rows_read = 0
    rows_without_issn = 0
    issn_rows_written = 0
    unranked = 0

    for row in iter_rows(csv_path):
        rows_read += 1
        issns = split_issns(row.get("Issn"))
        if not issns:
            rows_without_issn += 1
            continue

        quartile = parse_quartile(row.get("SJR Best Quartile"))
        if quartile is None:
            unranked += 1
        sjr = parse_decimal(row.get("SJR"))
        title = (row.get("Title") or "").strip()

        # Une entrée par ISSN : PubMed peut renvoyer l'un ou l'autre.
        conn.executemany(
            _UPSERT, [(issn, title, quartile, sjr, now) for issn in issns]
        )
        issn_rows_written += len(issns)

    return ImportReport(rows_read, rows_without_issn, issn_rows_written, unranked)


def main(argv: list[str] | None = None) -> int:
    # La console Windows est en cp1252 par défaut : les accents des rapports
    # seraient illisibles sans cela.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path, help="chemin du CSV Scimago")
    args = parser.parse_args(argv)

    if not args.csv_path.is_file():
        print(f"Fichier introuvable : {args.csv_path}", file=sys.stderr)
        return 1

    init_db()
    with get_conn() as conn:
        report = import_csv(args.csv_path, conn)
    print(report.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
