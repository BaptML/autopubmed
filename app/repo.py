"""Fonctions de dépôt : tout l'accès SQL du projet passe par ici."""

from __future__ import annotations

import sqlite3

from app.db import utcnow_iso
from app.models import ArticleSummary, JournalQuality, SavedQuery

_QUERY_COLUMNS = (
    "id, name, folder, pubmed_query, quartile_min, citedness_min, "
    "sort, created_at, last_refreshed_at"
)


def _to_query(row: sqlite3.Row) -> SavedQuery:
    return SavedQuery(
        id=row["id"],
        name=row["name"],
        folder=row["folder"],
        pubmed_query=row["pubmed_query"],
        quartile_min=row["quartile_min"],
        citedness_min=row["citedness_min"],
        sort=row["sort"],
        created_at=row["created_at"],
        last_refreshed_at=row["last_refreshed_at"],
    )


# --- Requêtes sauvegardées ----------------------------------------------


def create_query(
    conn: sqlite3.Connection,
    *,
    name: str,
    pubmed_query: str,
    folder: str = "Sans dossier",
    quartile_min: int | None = None,
    citedness_min: float | None = None,
    sort: str = "date_desc",
) -> int:
    cursor = conn.execute(
        "INSERT INTO queries (name, folder, pubmed_query, quartile_min, "
        "citedness_min, sort, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, folder, pubmed_query, quartile_min, citedness_min, sort, utcnow_iso()),
    )
    return int(cursor.lastrowid or 0)


def get_query(conn: sqlite3.Connection, query_id: int) -> SavedQuery | None:
    row = conn.execute(
        f"SELECT {_QUERY_COLUMNS} FROM queries WHERE id = ?", (query_id,)
    ).fetchone()
    return _to_query(row) if row else None


def list_queries(conn: sqlite3.Connection) -> list[SavedQuery]:
    rows = conn.execute(
        f"SELECT {_QUERY_COLUMNS} FROM queries ORDER BY folder, name"
    ).fetchall()
    return [_to_query(row) for row in rows]


def update_query(
    conn: sqlite3.Connection,
    query_id: int,
    *,
    name: str,
    pubmed_query: str,
    folder: str,
    quartile_min: int | None,
    citedness_min: float | None,
    sort: str,
) -> None:
    """Met à jour une requête. La chaîne PubMed est enregistrée telle quelle."""
    conn.execute(
        "UPDATE queries SET name = ?, folder = ?, pubmed_query = ?, "
        "quartile_min = ?, citedness_min = ?, sort = ? WHERE id = ?",
        (name, folder, pubmed_query, quartile_min, citedness_min, sort, query_id),
    )


def delete_query(conn: sqlite3.Connection, query_id: int) -> None:
    """Supprime une requête et ses rattachements ; les articles restent en cache."""
    conn.execute("DELETE FROM query_articles WHERE query_id = ?", (query_id,))
    conn.execute("DELETE FROM queries WHERE id = ?", (query_id,))


def list_folders(conn: sqlite3.Connection) -> list[str]:
    """Dossiers existants, pour proposer l'auto-complétion du formulaire."""
    rows = conn.execute(
        "SELECT DISTINCT folder FROM queries ORDER BY folder COLLATE NOCASE"
    ).fetchall()
    return [row["folder"] for row in rows]


def mark_refreshed(conn: sqlite3.Connection, query_id: int) -> None:
    conn.execute(
        "UPDATE queries SET last_refreshed_at = ? WHERE id = ?",
        (utcnow_iso(), query_id),
    )


# --- Articles ------------------------------------------------------------


def upsert_article(conn: sqlite3.Connection, summary: ArticleSummary) -> None:
    """Insère ou rafraîchit les métadonnées. Le résumé déjà en cache est conservé."""
    conn.execute(
        """
        INSERT INTO articles (pmid, title, journal, issn, eissn, pub_date, url, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(pmid) DO UPDATE SET
            title = excluded.title,
            journal = excluded.journal,
            issn = excluded.issn,
            eissn = excluded.eissn,
            pub_date = excluded.pub_date,
            fetched_at = excluded.fetched_at
        """,
        (
            summary.pmid,
            summary.title,
            summary.journal,
            summary.issn,
            summary.eissn,
            summary.pub_date,
            summary.url,
            utcnow_iso(),
        ),
    )


def known_pmids(conn: sqlite3.Connection, pmids: list[str]) -> set[str]:
    """PMID déjà présents dans `articles` — inutile de les réinterroger."""
    if not pmids:
        return set()
    placeholders = ",".join("?" * len(pmids))
    rows = conn.execute(
        f"SELECT pmid FROM articles WHERE pmid IN ({placeholders})", pmids
    ).fetchall()
    return {row["pmid"] for row in rows}


def get_article_summary(conn: sqlite3.Connection, pmid: str) -> ArticleSummary | None:
    row = conn.execute(
        "SELECT pmid, title, journal, issn, eissn, pub_date FROM articles WHERE pmid = ?",
        (pmid,),
    ).fetchone()
    if row is None:
        return None
    return ArticleSummary(
        pmid=row["pmid"],
        title=row["title"],
        journal=row["journal"],
        issn=row["issn"],
        eissn=row["eissn"],
        pub_date=row["pub_date"],
    )


# --- Lien requête ↔ article ---------------------------------------------


def link_article(conn: sqlite3.Connection, query_id: int, pmid: str) -> bool:
    """Rattache un article à une requête. True si c'est une nouvelle paire.

    `first_seen` n'est jamais réécrit : c'est lui qui fonde le badge « non lu ».
    """
    cursor = conn.execute(
        "INSERT OR IGNORE INTO query_articles (query_id, pmid, first_seen, read) "
        "VALUES (?, ?, ?, 0)",
        (query_id, pmid, utcnow_iso()),
    )
    return cursor.rowcount > 0


# --- Lectures pour l'interface -------------------------------------------

# Le tri vient de la base : on ne concatène jamais sa valeur brute en SQL.
_SORT_CLAUSES = {
    "date_desc": "a.pub_date DESC, a.pmid DESC",
    "date_asc": "a.pub_date ASC, a.pmid ASC",
    "journal": "a.journal ASC, a.pub_date DESC",
}

# Meilleur quartile parmi les ISSN de l'article : c'est la même double
# tentative que dans le pipeline, exprimée en SQL.
_QUARTILE_SUBQUERY = """
    (SELECT MIN(jq.quartile) FROM journal_quality jq
      WHERE jq.issn IN (a.issn, a.eissn)) AS quartile
"""


def folders_with_queries(conn: sqlite3.Connection) -> list[dict]:
    """Arborescence de la barre latérale : dossiers, requêtes, non-lus."""
    rows = conn.execute(
        """
        SELECT q.id, q.name, q.folder, q.last_refreshed_at,
               COUNT(CASE WHEN qa.read = 0 THEN 1 END) AS unread,
               COUNT(qa.pmid) AS total
        FROM queries q
        LEFT JOIN query_articles qa ON qa.query_id = q.id
        GROUP BY q.id
        ORDER BY q.folder COLLATE NOCASE, q.name COLLATE NOCASE
        """
    ).fetchall()

    folders: dict[str, list[dict]] = {}
    for row in rows:
        folders.setdefault(row["folder"], []).append(dict(row))
    return [{"name": name, "queries": queries} for name, queries in folders.items()]


def list_articles_for_query(
    conn: sqlite3.Connection, query_id: int, sort: str = "date_desc"
) -> list[dict]:
    """Articles d'une requête, tels quels depuis le cache — aucun appel réseau."""
    order_by = _SORT_CLAUSES.get(sort, _SORT_CLAUSES["date_desc"])
    rows = conn.execute(
        f"""
        SELECT a.pmid, a.title, a.journal, a.pub_date, a.url,
               a.abstract IS NOT NULL AS has_abstract,
               qa.read, qa.first_seen,
               {_QUARTILE_SUBQUERY}
        FROM query_articles qa
        JOIN articles a ON a.pmid = qa.pmid
        WHERE qa.query_id = ?
        ORDER BY {order_by}
        """,
        (query_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def count_unread(conn: sqlite3.Connection, query_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM query_articles WHERE query_id = ? AND read = 0",
        (query_id,),
    ).fetchone()
    return int(row["n"])


# --- Qualité des revues --------------------------------------------------


def get_quality_map(
    conn: sqlite3.Connection, issns: list[str]
) -> dict[str, JournalQuality]:
    """Verdict qualité pour un lot d'ISSN, en une seule requête."""
    if not issns:
        return {}
    placeholders = ",".join("?" * len(issns))
    rows = conn.execute(
        f"SELECT issn, quartile, citedness FROM journal_quality "
        f"WHERE issn IN ({placeholders})",
        issns,
    ).fetchall()
    return {
        row["issn"]: JournalQuality(quartile=row["quartile"], citedness=row["citedness"])
        for row in rows
    }


def set_citedness(
    conn: sqlite3.Connection, issn: str, citedness: float | None, journal_name: str | None
) -> None:
    """Mémorise la citedness OpenAlex sans toucher au quartile Scimago.

    La ligne est créée si la revue est absente de Scimago : on saura alors
    qu'elle a déjà été cherchée (citedness renseignée ou non).
    """
    conn.execute(
        """
        INSERT INTO journal_quality (issn, journal_name, citedness, source, updated_at)
        VALUES (?, ?, ?, 'openalex', ?)
        ON CONFLICT(issn) DO UPDATE SET
            citedness = excluded.citedness,
            journal_name = COALESCE(journal_quality.journal_name, excluded.journal_name),
            updated_at = excluded.updated_at
        """,
        (issn, journal_name, citedness, utcnow_iso()),
    )


def issns_missing_citedness(conn: sqlite3.Connection, issns: list[str]) -> list[str]:
    """ISSN sans citedness connue.

    Une revue absente d'OpenAlex sera réinterrogée aux rafraîchissements
    suivants — c'est assumé : le pipeline n'appelle OpenAlex que lorsqu'un
    filtre citedness est actif, et ces revues sont rares.
    """
    if not issns:
        return []
    placeholders = ",".join("?" * len(issns))
    rows = conn.execute(
        f"SELECT issn FROM journal_quality "
        f"WHERE issn IN ({placeholders}) AND citedness IS NOT NULL",
        issns,
    ).fetchall()
    already = {row["issn"] for row in rows}
    return [issn for issn in issns if issn not in already]
