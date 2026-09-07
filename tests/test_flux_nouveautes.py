"""Tests du flux transversal des nouveautés."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app import repo
from app.db import get_conn, init_db
from app.models import ArticleSummary


def _il_y_a(minutes: int) -> str:
    moment = datetime.now(UTC) - timedelta(minutes=minutes)
    return moment.replace(microsecond=0).isoformat()


def _ajouter(conn, query_id: int, pmid: str, first_seen: str, **kwargs) -> None:
    champs = {
        "pmid": pmid,
        "title": f"Titre {pmid}",
        "journal": "N Engl J Med",
        "issn": "00284793",
        "eissn": None,
        "pub_date": "2026-01-15",
    }
    champs.update(kwargs)
    repo.upsert_article(conn, ArticleSummary(**champs))
    repo.link_article(conn, query_id, pmid)
    conn.execute(
        "UPDATE query_articles SET first_seen = ? WHERE query_id = ? AND pmid = ?",
        (first_seen, query_id, pmid),
    )


@pytest.fixture
def bac(tmp_path: Path):
    """Deux requêtes visitées il y a une heure, chacune avec un article récent."""
    path = tmp_path / "flux.db"
    init_db(path)
    with get_conn(path) as conn:
        a = repo.create_query(conn, name="Sepsis", folder="Réa", pubmed_query="x")
        b = repo.create_query(conn, name="Trauma", folder="Réa", pubmed_query="y")
        conn.execute(
            "UPDATE queries SET last_visited_at = ?, previous_visit_at = ?",
            (_il_y_a(minutes=60), _il_y_a(minutes=60)),
        )
        _ajouter(conn, a, "1", first_seen=_il_y_a(minutes=10))
        _ajouter(conn, b, "2", first_seen=_il_y_a(minutes=5))
        yield conn, a, b


def test_le_flux_traverse_les_requetes(bac) -> None:
    conn, _, _ = bac
    assert {a["pmid"] for a in repo.list_new_articles(conn)} == {"1", "2"}


def test_le_flux_va_du_plus_recent_au_plus_ancien(bac) -> None:
    """Trié sur l'arrivée, pas sur la publication : c'est un flux."""
    conn, _, _ = bac
    assert [a["pmid"] for a in repo.list_new_articles(conn)] == ["2", "1"]


def test_le_flux_porte_la_provenance(bac) -> None:
    conn, _, _ = bac
    premier = repo.list_new_articles(conn)[0]
    assert premier["query_name"] == "Trauma"
    assert premier["query_folder"] == "Réa"


def test_le_flux_ignore_ce_qui_precede_le_repere(bac) -> None:
    conn, a, _ = bac
    _ajouter(conn, a, "3", first_seen=_il_y_a(minutes=500))
    assert "3" not in {art["pmid"] for art in repo.list_new_articles(conn)}


def test_le_flux_ignore_une_requete_jamais_visitee(bac) -> None:
    """Sans repère, on ne présume pas de ce que l'utilisateur a déjà vu."""
    conn, _, _ = bac
    neuve = repo.create_query(conn, name="Neuve", pubmed_query="z")
    _ajouter(conn, neuve, "9", first_seen=_il_y_a(minutes=1))
    assert "9" not in {art["pmid"] for art in repo.list_new_articles(conn)}


def test_le_flux_respecte_l_anciennete_de_chaque_requete(bac) -> None:
    """Chaque requête garde son propre réglage : le flux ne l'uniformise pas."""
    conn, a, _ = bac
    _ajouter(conn, a, "3", first_seen=_il_y_a(minutes=1), pub_date="1998-04-02")
    assert "3" in {art["pmid"] for art in repo.list_new_articles(conn)}

    conn.execute("UPDATE queries SET max_age_years = 1 WHERE id = ?", (a,))
    assert "3" not in {art["pmid"] for art in repo.list_new_articles(conn)}


def test_le_flux_ecarte_les_publications_futures(bac) -> None:
    conn, a, _ = bac
    _ajouter(conn, a, "3", first_seen=_il_y_a(minutes=1), pub_date="2999-01-01")
    assert "3" not in {art["pmid"] for art in repo.list_new_articles(conn)}
    assert repo.count_new_articles(conn) == 2


def test_un_article_partage_apparait_une_fois_par_requete(bac) -> None:
    """La lecture se suit par paire (requête, article), pas par article."""
    conn, _, b = bac
    _ajouter(conn, b, "1", first_seen=_il_y_a(minutes=3))
    lignes = [a for a in repo.list_new_articles(conn) if a["pmid"] == "1"]
    assert len(lignes) == 2
    assert {ligne["query_name"] for ligne in lignes} == {"Sepsis", "Trauma"}


def test_le_compteur_suit_le_flux(bac) -> None:
    conn, _, _ = bac
    assert repo.count_new_articles(conn) == len(repo.list_new_articles(conn)) == 2


def test_vider_le_flux_le_laisse_vide(bac) -> None:
    conn, _, _ = bac
    repo.dismiss_new_articles(conn)
    assert repo.list_new_articles(conn) == []
    assert repo.count_new_articles(conn) == 0


def test_vider_le_flux_ne_marque_rien_comme_lu(bac) -> None:
    """« Vu » n'est pas « lu » : les articles restent à lire."""
    conn, a, _ = bac
    repo.dismiss_new_articles(conn)
    assert repo.count_unread(conn, a) == 1


def test_vider_le_flux_dote_d_un_repere_les_requetes_neuves(bac) -> None:
    """Sans ça, une requête jamais ouverte resterait muette après une purge."""
    conn, _, _ = bac
    neuve = repo.create_query(conn, name="Neuve", pubmed_query="z")
    repo.dismiss_new_articles(conn)

    # Un rafraîchissement survenu après la purge.
    _ajouter(conn, neuve, "9", first_seen=_il_y_a(minutes=-1))
    assert "9" in {art["pmid"] for art in repo.list_new_articles(conn)}
