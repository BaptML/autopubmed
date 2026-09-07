"""Tests de la recherche plein texte dans le cache local."""

from pathlib import Path

import pytest

from app import repo
from app.db import get_conn, init_db
from app.models import ArticleSummary


def _ajouter(conn, query_id: int | None, pmid: str, **kwargs) -> None:
    champs = dict(
        pmid=pmid,
        title=f"Titre {pmid}",
        journal="N Engl J Med",
        issn="00284793",
        eissn=None,
        pub_date="2026-01-15",
    )
    champs.update(kwargs)
    repo.upsert_article(conn, ArticleSummary(**champs))
    if query_id is not None:
        repo.link_article(conn, query_id, pmid)


@pytest.fixture
def bac(tmp_path: Path):
    path = tmp_path / "recherche.db"
    init_db(path)
    with get_conn(path) as conn:
        query_id = repo.create_query(conn, name="A", pubmed_query="x")
        _ajouter(
            conn,
            query_id,
            "1",
            title="Early sepsis recognition in the emergency department",
            journal_full="New England Journal of Medicine",
            authors="Dupont J; Martin P",
        )
        _ajouter(
            conn,
            query_id,
            "2",
            title="Réanimation pédiatrique et sédation prolongée",
            journal_full="Intensive Care Medicine",
            authors="O'Brien K",
        )
        yield conn, query_id


def _pmids(conn, saisie: str) -> list[str]:
    return [a["pmid"] for a in repo.search_articles(conn, saisie)]


# --- Expression FTS -------------------------------------------------------


def test_les_termes_sont_joints_par_un_et_implicite() -> None:
    assert repo.to_match_expression("early sepsis") == '"early" "sepsis"*'


def test_le_dernier_terme_recoit_un_joker() -> None:
    """On cherche en tapant : « seps » doit déjà ramener « sepsis »."""
    assert repo.to_match_expression("seps").endswith('"*')


def test_un_terme_d_une_lettre_n_est_pas_jokerise() -> None:
    assert repo.to_match_expression("a") == '"a"'


def test_une_saisie_vide_ne_donne_pas_d_expression() -> None:
    assert repo.to_match_expression("   ") == ""


def test_les_guillemets_saisis_sont_neutralises() -> None:
    assert repo.to_match_expression('"sepsis"') == '"sepsis"*'


# --- Recherche ------------------------------------------------------------


def test_recherche_dans_le_titre(bac) -> None:
    conn, _ = bac
    assert _pmids(conn, "sepsis") == ["1"]


def test_recherche_dans_la_revue(bac) -> None:
    conn, _ = bac
    assert _pmids(conn, "intensive care") == ["2"]


def test_recherche_dans_les_auteurs(bac) -> None:
    conn, _ = bac
    assert _pmids(conn, "dupont") == ["1"]


def test_recherche_dans_un_resume_deja_lu(bac) -> None:
    """Les résumés arrivent à la demande : l'index s'enrichit au fil des lectures."""
    conn, _ = bac
    assert _pmids(conn, "vasopresseur") == []
    repo.set_abstract(conn, "1", "Recours précoce au vasopresseur.")
    assert _pmids(conn, "vasopresseur") == ["1"]


def test_la_recherche_ignore_les_accents(bac) -> None:
    conn, _ = bac
    assert _pmids(conn, "reanimation pediatrique") == ["2"]


def test_la_recherche_ignore_la_casse(bac) -> None:
    conn, _ = bac
    assert _pmids(conn, "SEPSIS") == ["1"]


def test_le_prefixe_suffit(bac) -> None:
    conn, _ = bac
    assert _pmids(conn, "seps") == ["1"]


def test_tous_les_termes_doivent_correspondre(bac) -> None:
    conn, _ = bac
    assert _pmids(conn, "sepsis emergency") == ["1"]
    assert _pmids(conn, "sepsis pédiatrique") == []


def test_une_saisie_vide_ne_ramene_rien(bac) -> None:
    conn, _ = bac
    assert repo.search_articles(conn, "   ") == []


def test_une_apostrophe_ne_fait_pas_lever(bac) -> None:
    """La syntaxe FTS5 a ses opérateurs : une saisie de bonne foi ne doit pas casser."""
    conn, _ = bac
    assert _pmids(conn, "O'Brien") == ["2"]


@pytest.mark.parametrize(
    "saisie", ["AND", "sepsis AND", "NOT OR", '"', "*", "-", "((", "^", ":"]
)
def test_aucune_saisie_ne_fait_lever(bac, saisie: str) -> None:
    conn, _ = bac
    repo.search_articles(conn, saisie)


def test_la_recherche_ecarte_les_publications_futures(bac) -> None:
    conn, query_id = bac
    _ajouter(
        conn, query_id, "3",
        title="Early futuristic sepsis breakthrough", pub_date="2999-01-01",
    )
    assert _pmids(conn, "futuristic") == []
    assert repo.count_search_results(conn, "sepsis") == 1


def test_un_article_partage_n_apparait_qu_une_fois(bac) -> None:
    """Contrairement au flux : ici on cherche un article, pas une paire."""
    conn, query_id = bac
    autre = repo.create_query(conn, name="B", pubmed_query="y")
    repo.link_article(conn, autre, "1")
    assert _pmids(conn, "sepsis") == ["1"]


def test_un_article_orphelin_reste_trouvable(bac) -> None:
    """Le cache garde des articles qu'aucune requête ne porte plus."""
    conn, _ = bac
    conn.execute("DELETE FROM query_articles")
    resultats = repo.search_articles(conn, "sepsis")
    assert [a["pmid"] for a in resultats] == ["1"]
    assert resultats[0]["query_id"] is None
