"""Accès SQLite : une connexion par requête HTTP ou par tâche de fond.

Le mode WAL permet au job de fond d'écrire pendant que le serveur lit.
Les connexions ne sont jamais partagées entre threads.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.config import PROJECT_ROOT, settings

SCHEMA_PATH: Path = PROJECT_ROOT / "app" / "schema.sql"


def utcnow_iso() -> str:
    """Horodatage ISO 8601 à la seconde, en UTC — format stocké partout."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Ouvre une connexion configurée. À fermer par l'appelant."""
    path = db_path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False : FastAPI sert les dépendances synchrones depuis
    # un pool de threads, et rien ne garantit que la dépendance et la vue
    # tournent sur le même. La connexion reste privée à une requête, donc
    # jamais utilisée par deux threads à la fois.
    conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL : lecteurs et écrivain concurrents sans blocage mutuel.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Laisse le temps au job de fond de libérer un verrou d'écriture.
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def get_conn(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Connexion transactionnelle : commit si tout va bien, rollback sinon."""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Colonnes ajoutées après coup. `schema.sql` est en CREATE TABLE IF NOT EXISTS :
# rejoué sur une base existante, il ne touche à rien, donc une nouvelle colonne
# n'y arriverait jamais. SQLite n'a pas d'ADD COLUMN IF NOT EXISTS, d'où cette
# table de rattrapage — les prochains ajouts se branchent ici.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("queries", "max_age_years", "INTEGER"),
    ("articles", "doi", "TEXT"),
)


def _add_missing_columns(conn: sqlite3.Connection) -> list[str]:
    """Ajoute les colonnes absentes. Idempotent. Renvoie celles qui ont été créées."""
    added: list[str] = []
    for table, column, definition in _ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            added.append(f"{table}.{column}")
    return added


def init_db(db_path: Path | None = None) -> Path:
    """Crée les tables si besoin, puis rattrape les colonnes manquantes."""
    path = db_path or settings.db_path
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_conn(path) as conn:
        conn.executescript(schema)
        for column in _add_missing_columns(conn):
            print(f"Migration : colonne ajoutée — {column}")
    return path


if __name__ == "__main__":  # `uv run python -m app.db` initialise la base
    created = init_db()
    print(f"Base initialisée : {created}")
