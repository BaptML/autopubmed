"""Retrait et restauration de la couche « pertinence ».

La fonction ne fait qu'ajouter trois tables (`schema_relevance.sql`). La retirer
revient à les supprimer, après une sauvegarde complète de la base et un export
lisible des notes, qui restent ainsi réimportables.

Usage :
  uv run python -m app.relevance_admin --desinstaller
  uv run python -m app.relevance_admin --importer data/backups/notes-AAAAMMJJ-HHMMSS.json
  (option --db <chemin> pour viser une autre base, par exemple une copie)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings
from app.db import (
    RELEVANCE_SCHEMA_PATH,
    RELEVANCE_TABLES,
    _table_exists,
    backup_db,
    get_conn,
    utcnow_iso,
)

EXPORT_VERSION = 1


@dataclass(frozen=True, slots=True)
class UninstallReport:
    backup: Path
    export: Path
    ratings: int


@dataclass(frozen=True, slots=True)
class ImportReport:
    ratings: int
    settings: int
    skipped: int  # requête ou article disparus depuis l'export


def export_ratings(db_path: Path) -> dict:
    """Notes et réglages, avec titres en clair pour rester lisibles à l'œil."""
    with get_conn(db_path) as conn:
        ratings = [
            dict(row)
            for row in conn.execute(
                """
                SELECT r.query_id, q.name AS query_name, r.pmid,
                       a.title, r.rating, r.rated_at
                FROM article_ratings r
                JOIN queries q ON q.id = r.query_id
                JOIN articles a ON a.pmid = r.pmid
                ORDER BY r.query_id, r.rated_at
                """
            )
        ]
        reglages = [
            dict(row)
            for row in conn.execute(
                "SELECT query_id, hide_below FROM relevance_settings ORDER BY query_id"
            )
        ]
    return {
        "version": EXPORT_VERSION,
        "exported_at": utcnow_iso(),
        "ratings": ratings,
        "settings": reglages,
    }


def uninstall(db_path: Path) -> UninstallReport | None:
    """Sauvegarde, exporte les notes, puis supprime les tables. None si absentes."""
    with get_conn(db_path) as conn:
        if not any(_table_exists(conn, table) for table in RELEVANCE_TABLES):
            return None
        complete = all(_table_exists(conn, table) for table in RELEVANCE_TABLES)
    if not complete:
        # Installation partielle (interrompue) : rien de fiable à exporter.
        raise RuntimeError("Tables de pertinence incomplètes : retrait manuel requis.")

    backup = backup_db(db_path)
    contenu = export_ratings(db_path)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    export = backup.parent / f"notes-{stamp}.json"
    export.write_text(json.dumps(contenu, ensure_ascii=False, indent=2), "utf-8")

    with get_conn(db_path) as conn:
        for table in RELEVANCE_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    return UninstallReport(backup, export, len(contenu["ratings"]))


def import_ratings(db_path: Path, export_path: Path) -> ImportReport:
    """Réimporte un export. Ne remplace jamais une note déjà présente.

    Les prédictions ne sont pas exportées : elles se recalculent depuis les notes.
    """
    contenu = json.loads(export_path.read_text("utf-8"))
    if contenu.get("version") != EXPORT_VERSION:
        raise ValueError(f"Version d'export inconnue : {contenu.get('version')!r}")

    imported = skipped = reglages = 0
    with get_conn(db_path) as conn:
        conn.executescript(RELEVANCE_SCHEMA_PATH.read_text(encoding="utf-8"))
        # Une note dont la requête ou l'article a disparu violerait les clés
        # étrangères : on l'écarte et on la compte.
        for note in contenu["ratings"]:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO article_ratings (query_id, pmid, rating, rated_at)
                SELECT ?, ?, ?, ?
                WHERE EXISTS (SELECT 1 FROM queries WHERE id = ?)
                  AND EXISTS (SELECT 1 FROM articles WHERE pmid = ?)
                """,
                (
                    note["query_id"],
                    note["pmid"],
                    note["rating"],
                    note["rated_at"],
                    note["query_id"],
                    note["pmid"],
                ),
            )
            if cursor.rowcount:
                imported += 1
            elif not conn.execute(
                "SELECT 1 FROM article_ratings WHERE query_id = ? AND pmid = ?",
                (note["query_id"], note["pmid"]),
            ).fetchone():
                skipped += 1
        for reglage in contenu["settings"]:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO relevance_settings (query_id, hide_below)
                SELECT ?, ? WHERE EXISTS (SELECT 1 FROM queries WHERE id = ?)
                """,
                (reglage["query_id"], reglage["hide_below"], reglage["query_id"]),
            )
            reglages += cursor.rowcount
    return ImportReport(imported, reglages, skipped)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Retrait / restauration des notes")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--desinstaller",
        action="store_true",
        help="sauvegarder, exporter les notes puis supprimer les tables",
    )
    action.add_argument("--importer", type=Path, metavar="JSON", help="réimporter")
    parser.add_argument("--db", type=Path, default=None, help="base visée")
    args = parser.parse_args(argv)
    db_path = args.db or settings.db_path

    if args.desinstaller:
        report = uninstall(db_path)
        if report is None:
            print("Aucune table de pertinence dans cette base : rien à retirer.")
            return 0
        print(f"Sauvegarde de la base : {report.backup}")
        print(f"Notes exportées ({report.ratings}) : {report.export}")
        print("Tables de pertinence supprimées.")
        print(
            "Pour qu'elles ne soient pas recréées (vides) au prochain démarrage : "
            "ENABLE_RELEVANCE=0 dans .env, ou retour au code `avant-notation`."
        )
        return 0

    report = import_ratings(db_path, args.importer)
    print(
        f"Notes réimportées : {report.ratings} · réglages : {report.settings}"
        f" · ignorées (requête ou article disparus) : {report.skipped}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
