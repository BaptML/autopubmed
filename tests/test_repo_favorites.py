"""Tests des favoris au niveau du dépôt : marquage, filtre et bibliographie."""

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


@pytest.fixture
def bac(tmp_path: Path):
    """Base peuplée : une requête et trois articles rattachés."""
    path = tmp_path / "favoris.db"
    init_db(path)
    with get_conn(path) as connection:
        query_id = repo.create_query(connection, name="A", pubmed_query="x")
        for pmid in ("1", "2", "3"):
            repo.upsert_article(connection, _summary(pmid))
            repo.link_article(connection, query_id, pmid)
        yield connection, query_id


def test_set_favorite_ajoute_puis_retire(bac: tuple) -> None:
    conn, _ = bac
    repo.set_favorite(conn, "1", True)
    repo.set_favorite(conn, "1", True)  # idempotent
    assert repo.count_favorites(conn) == 1

    repo.set_favorite(conn, "1", False)
    assert repo.count_favorites(conn) == 0


def test_is_favorite_remonte_dans_la_liste_d_une_requete(bac: tuple) -> None:
    conn, query_id = bac
    repo.set_favorite(conn, "2", True)
    articles = {a["pmid"]: a for a in repo.list_articles_for_query(conn, query_id)}
    assert articles["2"]["is_favorite"] == 1
    assert articles["1"]["is_favorite"] == 0


def test_filtre_favoris_seulement(bac: tuple) -> None:
    conn, query_id = bac
    repo.set_favorite(conn, "2", True)
    articles = repo.list_articles_for_query(conn, query_id, favorites_only=True)
    assert [a["pmid"] for a in articles] == ["2"]


def test_liste_des_favoris_porte_la_requete_d_origine(bac: tuple) -> None:
    conn, query_id = bac
    repo.set_favorite(conn, "1", True)
    repo.set_favorite(conn, "3", True)
    favoris = repo.list_favorite_articles(conn)
    # Le plus récemment étoilé d'abord.
    assert [a["pmid"] for a in favoris] == ["3", "1"]
    assert all(a["query_id"] == query_id for a in favoris)
    assert favoris[0]["title"] == "Titre 3"


def test_un_favori_survit_a_la_suppression_de_sa_requete(bac: tuple) -> None:
    """La bibliographie ne doit pas se vider quand on nettoie ses requêtes."""
    conn, query_id = bac
    repo.set_favorite(conn, "1", True)
    repo.delete_query(conn, query_id)

    favoris = repo.list_favorite_articles(conn)
    assert [a["pmid"] for a in favoris] == ["1"]
    assert favoris[0]["query_id"] is None  # plus de requête où l'ouvrir


def test_favorites_missing_citation_ignore_les_articles_deja_interroges(
    bac: tuple,
) -> None:
    conn, _ = bac
    repo.upsert_article(conn, _summary("4", authors="Doe J; Roe A"))
    repo.set_favorite(conn, "4", True)
    repo.set_favorite(conn, "1", True)  # upsert sans auteurs : authors = ''
    conn.execute("UPDATE articles SET authors = NULL WHERE pmid = '2'")
    repo.set_favorite(conn, "2", True)

    assert repo.favorites_missing_citation(conn) == ["2"]


def test_upsert_n_efface_pas_les_metadonnees_de_citation(bac: tuple) -> None:
    conn, _ = bac
    repo.upsert_article(conn, _summary("1", authors="Doe J", volume="12", pages="1-9"))
    repo.upsert_article(conn, _summary("1"))  # rafraîchissement sans métadonnées

    article = repo.get_article_summary(conn, "1")
    assert article.authors == "Doe J"
    assert article.volume == "12"
    assert article.pages == "1-9"
