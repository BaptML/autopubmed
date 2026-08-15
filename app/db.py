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
    conn = sqlite3.connect(path, timeout=30.0)
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


def init_db(db_path: Path | None = None) -> Path:
    """Crée les tables si besoin. Renvoie le chemin de la base."""
    path = db_path or settings.db_path
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_conn(path) as conn:
        conn.executescript(schema)
    return path


if __name__ == "__main__":  # `uv run python -m app.db` initialise la base
    created = init_db()
    print(f"Base initialisée : {created}")
