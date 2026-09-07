"""Tests du repère de visite, qui fonde le badge « nouveau »."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app import repo
from app.db import get_conn, init_db
from app.models import ArticleSummary


def _il_y_a(minutes: int) -> str:
    moment = datetime.now(UTC) - timedelta(minutes=minutes)
    return moment.replace(microsecond=0).isoformat()


@pytest.fixture
def bac(tmp_path: Path):
    """Une requête vide, jamais visitée, créée il y a un moment.

    Sa création est reculée pour laisser de la place aux articles : le repère
    d'une première visite tombe dessus, et « arrivé après » doit pouvoir se dire.
    """
    path = tmp_path / "visites.db"
    init_db(path)
    with get_conn(path) as conn:
        query_id = repo.create_query(conn, name="A", pubmed_query="x")
        conn.execute(
            "UPDATE queries SET created_at = ? WHERE id = ?",
            (_il_y_a(minutes=1000), query_id),
        )
        yield conn, query_id


def _repere(conn, query_id: int) -> tuple[str | None, str | None]:
    row = conn.execute(
        "SELECT last_visited_at, previous_visit_at FROM queries WHERE id = ?",
        (query_id,),
    ).fetchone()
    return row["last_visited_at"], row["previous_visit_at"]


def _creation(conn, query_id: int) -> str:
    return conn.execute(
        "SELECT created_at FROM queries WHERE id = ?", (query_id,)
    ).fetchone()["created_at"]


def _ajouter_article(conn, query_id: int, pmid: str, first_seen: str) -> None:
    repo.upsert_article(
        conn,
        ArticleSummary(
            pmid=pmid,
            title=f"Titre {pmid}",
            journal="N Engl J Med",
            issn="00284793",
            eissn=None,
            pub_date="2026-01-15",
        ),
    )
    repo.link_article(conn, query_id, pmid)
    conn.execute(
        "UPDATE query_articles SET first_seen = ? WHERE query_id = ? AND pmid = ?",
        (first_seen, query_id, pmid),
    )


# --- Décalage du repère ---------------------------------------------------


def test_premiere_visite_cale_le_repere_sur_la_creation(bac) -> None:
    conn, query_id = bac
    repo.record_visit(conn, query_id)
    last, previous = _repere(conn, query_id)
    assert previous == _creation(conn, query_id)
    assert last is not None


def test_visite_rapprochee_ne_decale_pas_le_repere(bac) -> None:
    """Le cœur du choix : recharger la liste n'efface pas les badges affichés."""
    conn, query_id = bac
    repo.record_visit(conn, query_id)
    _, avant = _repere(conn, query_id)

    repo.record_visit(conn, query_id)
    _, apres = _repere(conn, query_id)
    assert apres == avant


def test_visite_apres_le_seuil_decale_le_repere(bac) -> None:
    conn, query_id = bac
    repo.record_visit(conn, query_id)
    veille = _il_y_a(minutes=120)
    conn.execute(
        "UPDATE queries SET last_visited_at = ? WHERE id = ?", (veille, query_id)
    )

    repo.record_visit(conn, query_id)
    last, previous = _repere(conn, query_id)
    assert previous == veille
    assert last > veille


def test_le_seuil_est_reglable(bac) -> None:
    conn, query_id = bac
    repo.record_visit(conn, query_id)
    tout_a_l_heure = _il_y_a(minutes=10)
    conn.execute(
        "UPDATE queries SET last_visited_at = ? WHERE id = ?",
        (tout_a_l_heure, query_id),
    )

    repo.record_visit(conn, query_id, gap_minutes=60)  # trop tôt
    _, previous = _repere(conn, query_id)
    assert previous != tout_a_l_heure

    # La visite a rafraîchi `last_visited_at` : on le repose pour rejouer.
    conn.execute(
        "UPDATE queries SET last_visited_at = ? WHERE id = ?",
        (tout_a_l_heure, query_id),
    )
    repo.record_visit(conn, query_id, gap_minutes=5)  # assez tard
    _, previous = _repere(conn, query_id)
    assert previous == tout_a_l_heure


def test_visite_d_une_requete_inconnue_ne_leve_pas(bac) -> None:
    conn, _ = bac
    repo.record_visit(conn, 999)


def test_horodatage_illisible_ouvre_une_session(bac) -> None:
    """Une date corrompue ne doit pas figer le repère pour toujours."""
    conn, query_id = bac
    conn.execute(
        "UPDATE queries SET last_visited_at = ? WHERE id = ?",
        ("n'importe quoi", query_id),
    )
    repo.record_visit(conn, query_id)
    _, previous = _repere(conn, query_id)
    assert previous == "n'importe quoi"


# --- Effet sur le badge ---------------------------------------------------


def test_article_arrive_apres_le_repere_est_nouveau(bac) -> None:
    conn, query_id = bac
    repo.record_visit(conn, query_id)
    _ajouter_article(conn, query_id, "1", first_seen=_il_y_a(minutes=1))

    articles = repo.list_articles_for_query(conn, query_id)
    assert [a["is_new"] for a in articles] == [1]


def test_article_arrive_avant_le_repere_ne_l_est_pas(bac) -> None:
    conn, query_id = bac
    _ajouter_article(conn, query_id, "1", first_seen=_il_y_a(minutes=500))
    conn.execute(
        "UPDATE queries SET previous_visit_at = ? WHERE id = ?",
        (_il_y_a(minutes=200), query_id),
    )

    articles = repo.list_articles_for_query(conn, query_id)
    assert [a["is_new"] for a in articles] == [0]


def test_sans_repere_rien_n_est_nouveau(bac) -> None:
    """Requête jamais ouverte : on ne présume pas de ce que l'utilisateur a vu."""
    conn, query_id = bac
    _ajouter_article(conn, query_id, "1", first_seen=_il_y_a(minutes=1))

    articles = repo.list_articles_for_query(conn, query_id)
    assert [a["is_new"] for a in articles] == [0]


def test_la_bibliographie_agrege_le_badge_sur_les_requetes(bac) -> None:
    """Un favori est nouveau s'il l'est dans au moins une requête qui le porte."""
    conn, query_id = bac
    autre = repo.create_query(conn, name="B", pubmed_query="y")
    _ajouter_article(conn, query_id, "1", first_seen=_il_y_a(minutes=500))
    repo.link_article(conn, autre, "1")
    conn.execute(
        "UPDATE query_articles SET first_seen = ? WHERE query_id = ?",
        (_il_y_a(minutes=1), autre),
    )
    # Seule la seconde requête a un repère antérieur à l'arrivée de l'article.
    conn.execute(
        "UPDATE queries SET previous_visit_at = ? WHERE id = ?",
        (_il_y_a(minutes=200), query_id),
    )
    conn.execute(
        "UPDATE queries SET previous_visit_at = ? WHERE id = ?",
        (_il_y_a(minutes=10), autre),
    )
    repo.set_favorite(conn, "1", True)

    favoris = repo.list_favorite_articles(conn)
    assert [a["is_new"] for a in favoris] == [1]


def test_un_favori_orphelin_reste_lisible(bac) -> None:
    """La jointure sur `queries` ne doit pas faire disparaître un favori isolé."""
    conn, query_id = bac
    _ajouter_article(conn, query_id, "1", first_seen=_il_y_a(minutes=5))
    repo.set_favorite(conn, "1", True)
    conn.execute("DELETE FROM query_articles WHERE pmid = ?", ("1",))

    favoris = repo.list_favorite_articles(conn)
    assert [a["pmid"] for a in favoris] == ["1"]
    assert favoris[0]["is_new"] == 0
