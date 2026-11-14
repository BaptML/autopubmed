"""Fonctions de dépôt : tout l'accès SQL du projet passe par ici."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

from app.config import settings
from app.db import relevance_enabled, utcnow_iso
from app.models import ArticleSummary, JournalQuality, SavedQuery

_QUERY_COLUMNS = (
    "id, name, folder, pubmed_query, quartile_min, citedness_min, "
    "max_age_years, sort, created_at, last_refreshed_at, "
    "last_visited_at, previous_visit_at"
)


def _to_query(row: sqlite3.Row) -> SavedQuery:
    return SavedQuery(
        id=row["id"],
        name=row["name"],
        folder=row["folder"],
        pubmed_query=row["pubmed_query"],
        quartile_min=row["quartile_min"],
        citedness_min=row["citedness_min"],
        max_age_years=row["max_age_years"],
        sort=row["sort"],
        created_at=row["created_at"],
        last_refreshed_at=row["last_refreshed_at"],
        last_visited_at=row["last_visited_at"],
        previous_visit_at=row["previous_visit_at"],
    )


def cutoff_date(max_age_years: int | None) -> str | None:
    """Date de publication la plus ancienne acceptée, en ISO. None = illimité.

    Fenêtre glissante : on retranche N années à aujourd'hui plutôt que de
    raisonner en années civiles, pour que « 1 an » veuille bien dire douze mois.
    """
    if not max_age_years:
        return None
    today = date.today()
    try:
        return today.replace(year=today.year - max_age_years).isoformat()
    except ValueError:  # 29 février d'une année bissextile
        return today.replace(year=today.year - max_age_years, day=28).isoformat()


# --- Requêtes sauvegardées ----------------------------------------------


def create_query(
    conn: sqlite3.Connection,
    *,
    name: str,
    pubmed_query: str,
    folder: str = "Sans dossier",
    quartile_min: int | None = None,
    citedness_min: float | None = None,
    max_age_years: int | None = None,
    sort: str = "date_desc",
) -> int:
    cursor = conn.execute(
        "INSERT INTO queries (name, folder, pubmed_query, quartile_min, "
        "citedness_min, max_age_years, sort, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            name,
            folder,
            pubmed_query,
            quartile_min,
            citedness_min,
            max_age_years,
            sort,
            utcnow_iso(),
        ),
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
    max_age_years: int | None,
    sort: str,
) -> None:
    """Met à jour une requête. La chaîne PubMed est enregistrée telle quelle."""
    conn.execute(
        "UPDATE queries SET name = ?, folder = ?, pubmed_query = ?, "
        "quartile_min = ?, citedness_min = ?, max_age_years = ?, sort = ? "
        "WHERE id = ?",
        (
            name,
            folder,
            pubmed_query,
            quartile_min,
            citedness_min,
            max_age_years,
            sort,
            query_id,
        ),
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
        INSERT INTO articles
            (pmid, title, journal, issn, eissn, pub_date, doi,
             authors, journal_full, volume, issue, pages, url, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(pmid) DO UPDATE SET
            title = excluded.title,
            journal = excluded.journal,
            issn = excluded.issn,
            eissn = excluded.eissn,
            pub_date = excluded.pub_date,
            -- Une réponse sans DOI ('') n'efface pas celui déjà connu, mais
            -- laisse la trace « interrogé » : plus de rattrapage inutile.
            doi = COALESCE(NULLIF(excluded.doi, ''), articles.doi, ''),
            authors = COALESCE(NULLIF(excluded.authors, ''), articles.authors, ''),
            journal_full = COALESCE(
                NULLIF(excluded.journal_full, ''), articles.journal_full, ''),
            volume = COALESCE(NULLIF(excluded.volume, ''), articles.volume, ''),
            issue = COALESCE(NULLIF(excluded.issue, ''), articles.issue, ''),
            pages = COALESCE(NULLIF(excluded.pages, ''), articles.pages, ''),
            fetched_at = excluded.fetched_at
        """,
        (
            summary.pmid,
            summary.title,
            summary.journal,
            summary.issn,
            summary.eissn,
            summary.pub_date,
            summary.doi or "",  # '' = esummary interrogé, aucun DOI publié
            summary.authors or "",
            summary.journal_full or "",
            summary.volume or "",
            summary.issue or "",
            summary.pages or "",
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


def pmids_missing_doi(conn: sqlite3.Connection, pmids: list[str]) -> set[str]:
    """PMID enregistrés avant l'ajout de la colonne `doi`, donc à réinterroger.

    Seul NULL compte : une chaîne vide signifie « esummary interrogé, aucun
    DOI publié », et ne doit pas relancer un appel à chaque rafraîchissement.
    """
    if not pmids:
        return set()
    placeholders = ",".join("?" * len(pmids))
    rows = conn.execute(
        f"SELECT pmid FROM articles WHERE doi IS NULL AND pmid IN ({placeholders})",
        pmids,
    ).fetchall()
    return {row["pmid"] for row in rows}


def get_article_summary(conn: sqlite3.Connection, pmid: str) -> ArticleSummary | None:
    row = conn.execute(
        "SELECT pmid, title, journal, issn, eissn, pub_date, doi, "
        "authors, journal_full, volume, issue, pages FROM articles WHERE pmid = ?",
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
        doi=row["doi"],
        authors=row["authors"],
        journal_full=row["journal_full"],
        volume=row["volume"],
        issue=row["issue"],
        pages=row["pages"],
    )


# --- Lien requête ↔ article ---------------------------------------------


def get_abstract(conn: sqlite3.Connection, pmid: str) -> str | None:
    """Résumé déjà en cache, ou None s'il n'a jamais été récupéré."""
    row = conn.execute(
        "SELECT abstract FROM articles WHERE pmid = ?", (pmid,)
    ).fetchone()
    return row["abstract"] if row else None


def set_abstract(conn: sqlite3.Connection, pmid: str, abstract: str | None) -> None:
    """Met le résumé en cache après un efetch."""
    conn.execute("UPDATE articles SET abstract = ? WHERE pmid = ?", (abstract, pmid))


def get_summary_fr(
    conn: sqlite3.Connection, pmid: str
) -> tuple[str | None, str | None]:
    """Résumé français en cache et le modèle qui l'a produit, sinon (None, None)."""
    row = conn.execute(
        "SELECT summary_fr, summary_model FROM articles WHERE pmid = ?", (pmid,)
    ).fetchone()
    if row is None:
        return None, None
    return row["summary_fr"], row["summary_model"]


def set_summary_fr(
    conn: sqlite3.Connection, pmid: str, summary: str, model: str
) -> None:
    """Met en cache le résumé français, en traçant le modèle et la date."""
    conn.execute(
        "UPDATE articles SET summary_fr = ?, summary_model = ?, summary_at = ? "
        "WHERE pmid = ?",
        (summary, model, utcnow_iso(), pmid),
    )


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
# « relevance » lit les alias `rating` et `predicted` de `_article_select` : la
# note fait foi, la prédiction vient à défaut, puis l'ordre chronologique. Sans
# note ni prédiction (couche éteinte ou modèle pas prêt), c'est `date_desc`.
_SORT_CLAUSES = {
    "date_desc": "a.pub_date DESC, a.pmid DESC",
    "date_asc": "a.pub_date ASC, a.pmid ASC",
    "journal": "a.journal ASC, a.pub_date DESC",
    "relevance": "COALESCE(rating, predicted) DESC NULLS LAST, "
    "a.pub_date DESC, a.pmid DESC",
}

# Meilleur quartile parmi les ISSN de l'article : c'est la même double
# tentative que dans le pipeline, exprimée en SQL.
_QUARTILE_SUBQUERY = """
    (SELECT MIN(jq.quartile) FROM journal_quality jq
      WHERE jq.issn IN (a.issn, a.eissn)) AS quartile
"""

# Filtres qualité de la requête `q`, réappliqués à la lecture comme
# l'ancienneté : durcir un filtre après coup vaut aussi pour les articles déjà
# rattachés, et le relâcher les fait réapparaître. Même règle que
# `quality.passes_quality` : une valeur inconnue rend la comparaison NULL, donc
# l'article est écarté dès qu'un filtre est actif.
_QUALITY_CLAUSE = """(
    (q.quartile_min IS NULL OR
     (SELECT MIN(jq.quartile) FROM journal_quality jq
       WHERE jq.issn IN (a.issn, a.eissn)) <= q.quartile_min)
    AND (q.citedness_min IS NULL OR
     (SELECT MAX(jq.citedness) FROM journal_quality jq
       WHERE jq.issn IN (a.issn, a.eissn)) >= q.citedness_min))"""


def _relevance_hidden() -> str:
    """Expression SQL vraie si la paire `qa` est masquée par la pertinence.

    Masqué : la note, ou à défaut la prédiction, est sous le seuil choisi pour
    la requête. Sans seuil ou sans note ni prédiction, la comparaison vaut NULL
    et l'article reste visible. Une seule définition pour la liste, son
    comptage, les non-lus et le flux : les compteurs ne peuvent pas contredire
    la liste. Couche éteinte : rien n'est masqué, et ses tables peuvent manquer.
    """
    if not relevance_enabled():
        return "0"
    return """IFNULL(
        COALESCE(
            (SELECT ar.rating FROM article_ratings ar
              WHERE ar.query_id = qa.query_id AND ar.pmid = qa.pmid),
            (SELECT rs.score FROM relevance_scores rs
              WHERE rs.query_id = qa.query_id AND rs.pmid = qa.pmid))
        < (SELECT st.hide_below FROM relevance_settings st
            WHERE st.query_id = qa.query_id),
        0)"""


def _relevance_visible() -> str:
    return f"NOT {_relevance_hidden()}"


def folders_with_queries(conn: sqlite3.Connection) -> list[dict]:
    """Arborescence de la barre latérale : dossiers, requêtes, non-lus."""
    rows = conn.execute(
        f"""
        SELECT q.id, q.name, q.folder, q.last_refreshed_at,
               COUNT(CASE WHEN qa.read = 0 AND {_LIST_VISIBLE}
                               AND {_relevance_visible()}
                          THEN 1 END) AS unread,
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


# Colonnes d'article communes à la liste d'une requête et à celle des favoris :
# les deux vues rendent le même gabarit de ligne, elles doivent donc offrir les
# mêmes clés.
_ARTICLE_COLUMNS = f"""
        a.pmid, a.title, a.journal, a.pub_date, a.url, a.doi,
        a.authors, a.journal_full, a.volume, a.issue, a.pages,
        a.abstract IS NOT NULL AS has_abstract,
        substr(a.abstract, 1, 300) AS abstract_excerpt,
        EXISTS(SELECT 1 FROM favorites f WHERE f.pmid = a.pmid) AS is_favorite,
        {_QUARTILE_SUBQUERY}
"""


# Un article est nouveau dans une requête si son rattachement est postérieur au
# repère de visite de celle-ci. Définie une fois, la clause sert au badge comme
# au flux des nouveautés, qui ne peuvent donc pas se contredire.
_NEW_CLAUSE = """(q.previous_visit_at IS NOT NULL
                  AND qa.first_seen > q.previous_visit_at)"""


# Taille d'une page de liste. Une requête large ramène des centaines
# d'articles : les rendre d'un bloc alourdit la page sans servir la lecture.
PAGE_SIZE = 50


def _limite(limit: int | None, offset: int) -> str:
    """Clause LIMIT/OFFSET. Les valeurs sont des entiers, jamais du texte reçu."""
    if limit is None:
        return ""
    return f"LIMIT {int(limit)} OFFSET {int(offset)}"


def _relevance_columns() -> str:
    """Note de l'utilisateur et pertinence prédite, pour la paire (requête, article).

    Sous-requêtes plutôt que jointures, pour que la couche désactivée se
    réduise à deux `NULL` : ses tables peuvent alors être absentes de la base.
    """
    if not relevance_enabled():
        return "NULL AS rating, NULL AS predicted, 0 AS is_hidden"
    return f"""(SELECT ar.rating FROM article_ratings ar
                WHERE ar.query_id = qa.query_id AND ar.pmid = qa.pmid) AS rating,
              (SELECT rs.score FROM relevance_scores rs
                WHERE rs.query_id = qa.query_id AND rs.pmid = qa.pmid) AS predicted,
              {_relevance_hidden()} AS is_hidden"""


def _article_select(
    where: str, order_by: str, extra: str = "", pagination: str = ""
) -> str:
    """Fabrique la requête de lecture d'articles, seule forme utilisée par l'UI.

    `is_new` compare `first_seen` au repère de visite de la requête, lu par
    jointure : les horodatages sont des chaînes ISO en UTC, donc comparables
    telles quelles. Le calcul vit dans le SQL et non chez l'appelant, pour que
    toutes les vues répondent la même chose sans avoir à se passer la frontière.
    """
    return f"""
        SELECT {_ARTICLE_COLUMNS},
               qa.read, qa.first_seen, qa.query_id, {_relevance_columns()},
               {_NEW_CLAUSE} AS is_new{extra}
        FROM query_articles qa
        JOIN articles a ON a.pmid = qa.pmid
        JOIN queries q ON q.id = qa.query_id
        WHERE {where}
        ORDER BY {order_by}
        {pagination}
    """


# Ancienneté. Les dates partielles de PubMed (« 2021 », « 2021-03 ») sont
# complétées avant comparaison : sans cela « 2021 » < « 2021-01-01 » en ordre
# lexicographique, et l'article serait écarté à tort. Une date absente ne permet
# pas d'affirmer que l'article est trop vieux : on le garde.
_AGE_CLAUSE = (
    "(? IS NULL OR a.pub_date IS NULL OR substr(a.pub_date || '-12-31', 1, 10) >= ?)"
)

# Borne haute : rien de daté après aujourd'hui (coquilles PubMed, numéros « à
# paraître »). Option stricte, symétrique de `_AGE_CLAUSE` — la date partielle
# est complétée vers la fin de période, si bien qu'une année seule ne repasse
# « publiée » qu'au 31 décembre. `date('now')` est en UTC, comme
# `_FLUX_AGE_CLAUSE`. Une date absente ne suffit pas à écarter l'article.
_NOT_FUTURE_CLAUSE = (
    "(a.pub_date IS NULL OR substr(a.pub_date || '-12-31', 1, 10) <= date('now'))"
)


def list_articles_for_query(
    conn: sqlite3.Connection,
    query_id: int,
    sort: str = "date_desc",
    max_age_years: int | None = None,
    favorites_only: bool = False,
    limit: int | None = None,
    offset: int = 0,
    show_hidden: bool = False,
) -> list[dict]:
    """Articles d'une requête, tels quels depuis le cache — aucun appel réseau."""
    order_by = _SORT_CLAUSES.get(sort, _SORT_CLAUSES["date_desc"])
    cutoff = cutoff_date(max_age_years)
    where = _where_for_query(favorites_only, show_hidden)
    rows = conn.execute(
        _article_select(where, order_by, pagination=_limite(limit, offset)),
        (query_id, cutoff, cutoff),
    ).fetchall()
    return [dict(row) for row in rows]


def _where_for_query(favorites_only: bool, show_hidden: bool = False) -> str:
    """Filtre d'une liste de requête. Partagé par la lecture et le comptage."""
    where = (
        f"qa.query_id = ? AND {_AGE_CLAUSE} AND {_NOT_FUTURE_CLAUSE} "
        f"AND {_QUALITY_CLAUSE}"
    )
    if favorites_only:
        where += " AND EXISTS(SELECT 1 FROM favorites f WHERE f.pmid = a.pmid)"
    if not show_hidden:
        where += f" AND {_relevance_visible()}"
    return where


def count_articles_for_query(
    conn: sqlite3.Connection,
    query_id: int,
    max_age_years: int | None = None,
    favorites_only: bool = False,
    show_hidden: bool = False,
) -> int:
    """Total d'une liste, pour l'afficher pendant qu'elle se charge par pages."""
    cutoff = cutoff_date(max_age_years)
    return conn.execute(
        f"""
        SELECT COUNT(*) AS n
        FROM query_articles qa
        JOIN articles a ON a.pmid = qa.pmid
        JOIN queries q ON q.id = qa.query_id
        WHERE {_where_for_query(favorites_only, show_hidden)}
        """,
        (query_id, cutoff, cutoff),
    ).fetchone()["n"]


def count_hidden_for_query(
    conn: sqlite3.Connection,
    query_id: int,
    max_age_years: int | None = None,
    favorites_only: bool = False,
) -> int:
    """Articles que la pertinence retire de la liste, pour le dire en en-tête."""
    return count_articles_for_query(
        conn, query_id, max_age_years, favorites_only, show_hidden=True
    ) - count_articles_for_query(conn, query_id, max_age_years, favorites_only)


def get_article_row(conn: sqlite3.Connection, query_id: int, pmid: str) -> dict | None:
    """Une seule ligne d'article, pour la re-rendre après un changement d'état."""
    row = conn.execute(
        _article_select("qa.query_id = ? AND qa.pmid = ?", "a.pmid"),
        (query_id, pmid),
    ).fetchone()
    return dict(row) if row else None


# --- Flux des nouveautés ---------------------------------------------------

# L'ancienneté maximale se lit sur la requête d'origine : le flux traverse des
# requêtes aux réglages différents, chacune garde le sien.
_FLUX_AGE_CLAUSE = (
    "(q.max_age_years IS NULL OR a.pub_date IS NULL "
    "OR substr(a.pub_date || '-12-31', 1, 10) >= "
    "date('now', '-' || q.max_age_years || ' years'))"
)


# Une paire `qa` vue sans jointure : même verdict que la liste (ancienneté de
# la requête, pas de futur, qualité), pour les compteurs de non-lus.
_LIST_VISIBLE = f"""EXISTS(
        SELECT 1 FROM articles a JOIN queries q ON q.id = qa.query_id
        WHERE a.pmid = qa.pmid AND {_FLUX_AGE_CLAUSE} AND {_NOT_FUTURE_CLAUSE}
          AND {_QUALITY_CLAUSE})"""


def _flux_where() -> str:
    """Filtre du flux, partagé par sa lecture et son compteur."""
    return (
        f"{_NEW_CLAUSE} AND {_FLUX_AGE_CLAUSE} AND {_NOT_FUTURE_CLAUSE} "
        f"AND {_QUALITY_CLAUSE} AND {_relevance_visible()}"
    )


def list_new_articles(
    conn: sqlite3.Connection, limit: int | None = None, offset: int = 0
) -> list[dict]:
    """Tout ce qui est arrivé depuis la dernière visite, requêtes confondues.

    Un article présent dans deux requêtes apparaît une fois par requête : la
    lecture et le repère se suivent par paire, pas par article.
    """
    rows = conn.execute(
        _article_select(
            where=_flux_where(),
            order_by="qa.first_seen DESC, a.pub_date DESC, a.pmid DESC",
            extra=", q.name AS query_name, q.folder AS query_folder",
            pagination=_limite(limit, offset),
        )
    ).fetchall()
    return [dict(row) for row in rows]


def count_new_articles(conn: sqlite3.Connection) -> int:
    """Compteur du flux, affiché en tête de barre latérale."""
    return conn.execute(
        f"""
        SELECT COUNT(*) AS n
        FROM query_articles qa
        JOIN articles a ON a.pmid = qa.pmid
        JOIN queries q ON q.id = qa.query_id
        WHERE {_flux_where()}
        """
    ).fetchone()["n"]


def dismiss_new_articles(conn: sqlite3.Connection) -> None:
    """Vide le flux, en calant toutes les requêtes sur maintenant.

    C'est le seul geste qui décale les repères en bloc : ouvrir le flux ne
    consomme rien, sans quoi le simple fait de le consulter l'effacerait.
    """
    now = utcnow_iso()
    conn.execute(
        "UPDATE queries SET previous_visit_at = ?, "
        "last_visited_at = COALESCE(last_visited_at, ?)",
        (now, now),
    )


# --- Recherche plein texte -------------------------------------------------


def to_match_expression(saisie: str) -> str:
    """Transforme une saisie libre en expression FTS5 sûre.

    La syntaxe FTS5 a ses propres opérateurs : une apostrophe, un tiret ou un
    « AND » saisi de bonne foi feraient lever SQLite. Chaque terme est donc mis
    entre guillemets, ce qui le rend littéral, et les termes sont joints par un
    ET implicite. Le dernier reçoit un joker : on cherche en tapant.
    """
    termes = [t for t in saisie.replace('"', " ").split() if t]
    if not termes:
        return ""
    cites = [f'"{terme}"' for terme in termes[:-1]]
    # Le joker se pose hors des guillemets, sinon il serait pris au pied de la
    # lettre. Un terme d'une seule lettre ne le reçoit pas : trop de bruit.
    dernier = termes[-1]
    cites.append(f'"{dernier}"*' if len(dernier) > 1 else f'"{dernier}"')
    return " ".join(cites)


def search_articles(
    conn: sqlite3.Connection, saisie: str, limit: int | None = None, offset: int = 0
) -> list[dict]:
    """Articles du cache local répondant à la saisie, les plus pertinents d'abord.

    Cherche dans les titres, revues et auteurs — et dans les résumés déjà
    récupérés, ceux-ci n'étant chargés qu'à la demande (§6 du spec).
    """
    expression = to_match_expression(saisie)
    if not expression:
        return []
    rows = conn.execute(
        f"""
        SELECT {_ARTICLE_COLUMNS},
               COALESCE(MAX(qa.read), 0) AS read,
               MIN(qa.first_seen) AS first_seen,
               MIN(qa.query_id) AS query_id,
               COALESCE(MAX({_NEW_CLAUSE}), 0) AS is_new
        FROM articles_fts
        JOIN articles a ON a.rowid = articles_fts.rowid
        LEFT JOIN query_articles qa ON qa.pmid = a.pmid
        LEFT JOIN queries q ON q.id = qa.query_id
        WHERE articles_fts MATCH ? AND {_NOT_FUTURE_CLAUSE}
        GROUP BY a.pmid
        ORDER BY MIN(articles_fts.rank), a.pub_date DESC
        {_limite(limit, offset)}
        """,
        (expression,),
    ).fetchall()
    return [dict(row) for row in rows]


def count_search_results(conn: sqlite3.Connection, saisie: str) -> int:
    """Total des résultats, pour dire ce qui reste à charger."""
    expression = to_match_expression(saisie)
    if not expression:
        return 0
    return conn.execute(
        f"SELECT COUNT(*) AS n FROM articles_fts "
        f"JOIN articles a ON a.rowid = articles_fts.rowid "
        f"WHERE articles_fts MATCH ? AND {_NOT_FUTURE_CLAUSE}",
        (expression,),
    ).fetchone()["n"]


# --- Favoris ---------------------------------------------------------------


def set_favorite(conn: sqlite3.Connection, pmid: str, favorite: bool) -> None:
    """Ajoute ou retire un article de la bibliographie. Global à l'article."""
    if favorite:
        conn.execute(
            "INSERT OR IGNORE INTO favorites (pmid, added_at) VALUES (?, ?)",
            (pmid, utcnow_iso()),
        )
    else:
        conn.execute("DELETE FROM favorites WHERE pmid = ?", (pmid,))


def count_favorites(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM favorites").fetchone()["n"]


def _grouped_article_select(where: str, order_by: str, join: str = "") -> str:
    """Lecture d'articles hors du contexte d'une requête : un article, une ligne.

    `read`, `first_seen` et `query_id` sont agrégés sur toutes les requêtes qui
    contiennent l'article ; le `LEFT JOIN` laisse passer celui qui n'appartient
    plus à aucune — sans lui, un favori disparaîtrait de la bibliographie dès
    qu'on supprime la requête qui l'avait ramené.
    """
    return f"""
        SELECT {_ARTICLE_COLUMNS},
               COALESCE(MAX(qa.read), 0) AS read,
               MIN(qa.first_seen) AS first_seen,
               MIN(qa.query_id) AS query_id,
               COALESCE(MAX(q.previous_visit_at IS NOT NULL
                            AND qa.first_seen > q.previous_visit_at), 0) AS is_new
        FROM articles a
        {join}
        LEFT JOIN query_articles qa ON qa.pmid = a.pmid
        LEFT JOIN queries q ON q.id = qa.query_id
        WHERE {where}
        GROUP BY a.pmid
        ORDER BY {order_by}
    """


def list_favorite_articles(conn: sqlite3.Connection) -> list[dict]:
    """Favoris, du plus récemment étoilé au plus ancien."""
    rows = conn.execute(
        _grouped_article_select(
            where="1 = 1",
            order_by="fav.added_at DESC, a.pmid DESC",
            join="JOIN favorites fav ON fav.pmid = a.pmid",
        )
    ).fetchall()
    return [dict(row) for row in rows]


def get_article_row_anywhere(conn: sqlite3.Connection, pmid: str) -> dict | None:
    """Ligne d'un article vue depuis la bibliographie, sans requête courante."""
    row = conn.execute(
        _grouped_article_select(where="a.pmid = ?", order_by="a.pmid"),
        (pmid,),
    ).fetchone()
    return dict(row) if row else None


def favorites_missing_citation(conn: sqlite3.Connection) -> list[str]:
    """Favoris dont les métadonnées de citation n'ont jamais été récupérées.

    `''` signifie « interrogé, aucun auteur publié » : seul NULL déclenche un
    nouvel appel.
    """
    rows = conn.execute(
        "SELECT fav.pmid FROM favorites fav JOIN articles a ON a.pmid = fav.pmid "
        "WHERE a.authors IS NULL ORDER BY fav.pmid"
    ).fetchall()
    return [row["pmid"] for row in rows]


# --- Suivi de lecture ------------------------------------------------------


def set_read(conn: sqlite3.Connection, query_id: int, pmid: str, read: bool) -> None:
    """Marque un article lu ou non lu. Le suivi est propre à chaque requête."""
    conn.execute(
        "UPDATE query_articles SET read = ? WHERE query_id = ? AND pmid = ?",
        (1 if read else 0, query_id, pmid),
    )


def _unread_where(show_hidden: bool) -> str:
    """Non-lus d'une requête : ceux que la liste montre, masqués compris ou non."""
    where = f"qa.query_id = ? AND qa.read = 0 AND {_LIST_VISIBLE}"
    return where if show_hidden else f"{where} AND {_relevance_visible()}"


def mark_all_read(
    conn: sqlite3.Connection, query_id: int, show_hidden: bool = False
) -> int:
    """Marque lus tous les articles d'une requête, et d'elle seule.

    Les articles masqués par la pertinence ne sont touchés que s'ils sont
    affichés : on ne marque lu que ce que l'on a sous les yeux, et un article
    réaffiché plus tard garde son état non lu.
    Renvoie le nombre d'articles réellement basculés.
    """
    cursor = conn.execute(
        f"UPDATE query_articles AS qa SET read = 1 WHERE {_unread_where(show_hidden)}",
        (query_id,),
    )
    return cursor.rowcount


def count_unread(
    conn: sqlite3.Connection, query_id: int, show_hidden: bool = False
) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM query_articles qa WHERE {_unread_where(show_hidden)}",
        (query_id,),
    ).fetchone()
    return int(row["n"])


# --- Notes de pertinence ----------------------------------------------------

# Échelle des notes et libellés affichés : une seule définition pour le
# contrôle des valeurs et pour l'interface.
RATING_LABELS: dict[int, str] = {1: "hors sujet", 2: "secondaire", 3: "pertinent"}
RATINGS: tuple[int, ...] = tuple(RATING_LABELS)


def set_rating(conn: sqlite3.Connection, query_id: int, pmid: str, rating: int) -> None:
    """Note un article dans une requête ; une nouvelle note remplace l'ancienne."""
    if rating not in RATINGS:
        raise ValueError(f"Note hors échelle : {rating}")
    conn.execute(
        "INSERT INTO article_ratings (query_id, pmid, rating, rated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT (query_id, pmid) "
        "DO UPDATE SET rating = excluded.rating, rated_at = excluded.rated_at",
        (query_id, pmid, rating, utcnow_iso()),
    )


def clear_rating(conn: sqlite3.Connection, query_id: int, pmid: str) -> None:
    """Retire la note : l'article redevient « non noté » dans cette requête."""
    conn.execute(
        "DELETE FROM article_ratings WHERE query_id = ? AND pmid = ?",
        (query_id, pmid),
    )


def ratings_for_query(conn: sqlite3.Connection, query_id: int) -> dict[str, int]:
    """Notes d'une requête, par PMID."""
    rows = conn.execute(
        "SELECT pmid, rating FROM article_ratings WHERE query_id = ?", (query_id,)
    ).fetchall()
    return {row["pmid"]: row["rating"] for row in rows}


def query_documents(
    conn: sqlite3.Connection, query_id: int
) -> list[tuple[str, str, str | None]]:
    """(PMID, titre, revue) de tous les articles rattachés à une requête.

    Corpus du modèle de pertinence : sans filtre d'âge ni de date, pour que la
    prédiction d'un article ne dépende pas des réglages d'affichage.
    """
    rows = conn.execute(
        "SELECT a.pmid, a.title, a.journal FROM query_articles qa "
        "JOIN articles a ON a.pmid = qa.pmid WHERE qa.query_id = ? ORDER BY a.pmid",
        (query_id,),
    ).fetchall()
    return [(row["pmid"], row["title"], row["journal"]) for row in rows]


def get_hide_below(conn: sqlite3.Connection, query_id: int) -> float | None:
    """Seuil de masquage d'une requête. None = rien n'est masqué."""
    if not relevance_enabled():
        return None
    row = conn.execute(
        "SELECT hide_below FROM relevance_settings WHERE query_id = ?", (query_id,)
    ).fetchone()
    return row["hide_below"] if row else None


def set_hide_below(
    conn: sqlite3.Connection, query_id: int, hide_below: float | None
) -> None:
    """Règle le seuil de masquage. None le retire : tout réapparaît, rien n'est perdu."""
    if hide_below is not None and not 1 < hide_below <= 3:
        raise ValueError(f"Seuil hors échelle : {hide_below}")
    conn.execute(
        "INSERT INTO relevance_settings (query_id, hide_below) VALUES (?, ?) "
        "ON CONFLICT (query_id) DO UPDATE SET hide_below = excluded.hide_below",
        (query_id, hide_below),
    )


def replace_scores(
    conn: sqlite3.Connection, query_id: int, scores: dict[str, float]
) -> None:
    """Remplace toutes les prédictions d'une requête. Vide = modèle inactif."""
    conn.execute("DELETE FROM relevance_scores WHERE query_id = ?", (query_id,))
    now = utcnow_iso()
    conn.executemany(
        "INSERT INTO relevance_scores (query_id, pmid, score, computed_at) "
        "VALUES (?, ?, ?, ?)",
        [(query_id, pmid, score, now) for pmid, score in scores.items()],
    )


# --- Repère de visite ---------------------------------------------------


def record_visit(
    conn: sqlite3.Connection, query_id: int, gap_minutes: int | None = None
) -> None:
    """Note l'ouverture d'une requête, en décalant son repère s'il y a lieu.

    Le repère (`previous_visit_at`) ne suit pas chaque ouverture : sans quoi
    recharger la liste effacerait les badges qu'on est en train de regarder. Il
    ne se décale qu'après une interruption d'au moins `gap_minutes`, ce qui vaut
    pour une nouvelle session de lecture.

    À la toute première ouverture, le repère tombe sur la création de la
    requête : tout ce que son premier rafraîchissement a ramené est nouveau.
    """
    row = conn.execute(
        "SELECT created_at, last_visited_at FROM queries WHERE id = ?", (query_id,)
    ).fetchone()
    if row is None:
        return

    gap = settings.visit_session_gap_minutes if gap_minutes is None else gap_minutes
    now = utcnow_iso()
    last = row["last_visited_at"]

    if last is None:
        marker = row["created_at"]
    elif _passe_le_seuil(last, gap):
        marker = last
    else:
        conn.execute(
            "UPDATE queries SET last_visited_at = ? WHERE id = ?", (now, query_id)
        )
        return

    conn.execute(
        "UPDATE queries SET last_visited_at = ?, previous_visit_at = ? WHERE id = ?",
        (now, marker, query_id),
    )


def _passe_le_seuil(last_visited_at: str, gap_minutes: int) -> bool:
    """Vrai si la dernière visite est assez ancienne pour ouvrir une session."""
    try:
        last = datetime.fromisoformat(last_visited_at)
    except ValueError:  # horodatage illisible : on repart d'une session propre
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return datetime.now(UTC) - last >= timedelta(minutes=gap_minutes)


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
        row["issn"]: JournalQuality(
            quartile=row["quartile"], citedness=row["citedness"]
        )
        for row in rows
    }


def set_citedness(
    conn: sqlite3.Connection,
    issn: str,
    citedness: float | None,
    journal_name: str | None,
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
