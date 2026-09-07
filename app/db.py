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
    ("articles", "authors", "TEXT"),
    ("articles", "journal_full", "TEXT"),
    ("articles", "volume", "TEXT"),
    ("articles", "issue", "TEXT"),
    ("articles", "pages", "TEXT"),
    ("queries", "last_visited_at", "TEXT"),
    ("queries", "previous_visit_at", "TEXT"),
)


def _missing_columns(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Colonnes de `_ADDED_COLUMNS` absentes de la base, dans l'ordre déclaré."""
    missing: list[tuple[str, str, str]] = []
    for table, column, definition in _ADDED_COLUMNS:
        if not _table_exists(conn, table):
            continue  # base neuve : schema.sql la créera complète
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            missing.append((table, column, definition))
    return missing


def _add_missing_columns(conn: sqlite3.Connection) -> list[str]:
    """Ajoute les colonnes absentes. Idempotent. Renvoie celles qui ont été créées."""
    added: list[str] = []
    for table, column, definition in _missing_columns(conn):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        added.append(f"{table}.{column}")
    return added


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


# Nettoyage unique. Des articles datés après aujourd'hui (coquilles PubMed,
# numéros « à paraître ») faussaient compteurs et tris. Le filtre d'ingestion
# empêche désormais les nouveaux d'être rattachés ; ceci vide le stock déjà en
# base. Joué une seule fois, tracé dans `app_state`.
_FUTURE_PURGE_KEY = "future_articles_purged"


def _future_purge_done(conn: sqlite3.Connection) -> bool:
    """Vrai si le nettoyage des articles datés dans le futur a déjà eu lieu."""
    if not _table_exists(conn, "app_state"):
        return False
    row = conn.execute(
        "SELECT 1 FROM app_state WHERE key = ?", (_FUTURE_PURGE_KEY,)
    ).fetchone()
    return row is not None


def _purge_future_articles(conn: sqlite3.Connection) -> int:
    """Retire les articles datés après aujourd'hui. Renvoie le nombre supprimé.

    Option stricte, alignée sur `repo._NOT_FUTURE_CLAUSE` : la date partielle est
    complétée vers la fin de période. Un favori garde sa ligne — il reste dans
    la bibliographie — mais perd ses rattachements aux requêtes, pour ne plus
    peser sur les compteurs. Les autres quittent le cache : la cascade nettoie
    `query_articles`, le trigger FTS suit.
    """
    future = "substr(pub_date || '-12-31', 1, 10) > date('now')"
    conn.execute(
        f"DELETE FROM query_articles WHERE pmid IN "
        f"(SELECT pmid FROM articles WHERE pub_date IS NOT NULL AND {future})"
    )
    cursor = conn.execute(
        f"DELETE FROM articles WHERE pub_date IS NOT NULL AND {future} "
        f"AND pmid NOT IN (SELECT pmid FROM favorites)"
    )
    return cursor.rowcount


def backup_db(db_path: Path) -> Path:
    """Copie la base dans `backups/`, horodatée, et renvoie le chemin.

    Passe par l'API `backup()` de sqlite3 et non par une copie de fichier : en
    mode WAL, le fichier `.db` seul est un instantané incomplet. Les sauvegardes
    vivent à côté de la base, donc dans le volume monté en conteneur.
    """
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    destination = backup_dir / f"{db_path.stem}-{stamp}.db"

    source = sqlite3.connect(db_path)
    try:
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return destination


def _needs_migration(conn: sqlite3.Connection) -> bool:
    """Vrai si la base existante n'est pas au dernier schéma."""
    return bool(_missing_columns(conn)) or not _table_exists(conn, "articles_fts")


def init_db(db_path: Path | None = None) -> Path:
    """Crée les tables si besoin, puis rattrape ce qui manque au schéma.

    Toute migration d'une base déjà peuplée est précédée d'une sauvegarde : le
    schéma n'évolue qu'en ajouts, mais la seule façon d'annuler des *données*
    est de reposer le fichier d'avant.
    """
    path = db_path or settings.db_path
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_conn(path) as conn:
        # L'état d'avant se lit avant d'écrire quoi que ce soit.
        fts_absent = not _table_exists(conn, "articles_fts")
        articles_present = _table_exists(conn, "articles")
        purge_pending = articles_present and not _future_purge_done(conn)
        if articles_present and (_needs_migration(conn) or purge_pending):
            print(f"Migration : sauvegarde prise — {backup_db(path)}")

        conn.executescript(schema)
        added = _add_missing_columns(conn)
        for column in added:
            print(f"Migration : colonne ajoutée — {column}")

        # Uniquement à l'instant de la migration : rejoué à chaque démarrage, ce
        # calage écraserait le repère d'une requête créée depuis.
        if "queries.previous_visit_at" in added:
            _seed_visits(conn)
        if fts_absent:
            # Contenu externe : 'rebuild' relit `articles` et refait l'index.
            conn.execute("INSERT INTO articles_fts(articles_fts) VALUES('rebuild')")
            print("Migration : index de recherche construit")

        if not _future_purge_done(conn):
            removed = _purge_future_articles(conn)
            conn.execute(
                "INSERT OR REPLACE INTO app_state (key, value, updated_at) "
                "VALUES (?, ?, ?)",
                (_FUTURE_PURGE_KEY, utcnow_iso(), utcnow_iso()),
            )
            if removed:
                print(f"Migration : {removed} article(s) daté(s) dans le futur retiré(s)")
    return path


def _seed_visits(conn: sqlite3.Connection) -> None:
    """Cale sur maintenant les requêtes présentes à l'instant de la migration.

    Sans ça, une requête antérieure verrait son repère tomber sur `created_at`
    et présenterait tout son historique comme nouveau.
    """
    now = utcnow_iso()
    conn.execute(
        "UPDATE queries SET last_visited_at = ?, previous_visit_at = ?",
        (now, now),
    )


if __name__ == "__main__":  # `uv run python -m app.db` initialise la base
    created = init_db()
    print(f"Base initialisée : {created}")
