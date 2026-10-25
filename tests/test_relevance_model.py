"""Moteur de pertinence : tokenisation, démarrage à froid, prédiction, évaluation."""

import asyncio
import dataclasses
from pathlib import Path

import pytest

from app import db, repo
from app.db import get_conn, init_db
from app.models import ArticleSummary, ESearchResult
from app.refresh import refresh_query
from app.relevance import (
    MIN_RATINGS,
    build_vectors,
    explain,
    features,
    learning_status,
    leave_one_out,
    predict_unrated,
    recompute_scores,
    tokenize,
)

# Corpus fabriqué : le choc septique est pertinent, l'asthme hors sujet.
PERTINENTS = {
    "p1": "Septic shock and vasopressor timing",
    "p2": "Norepinephrine dosing in septic shock mortality",
    "p3": "Vasopressin versus norepinephrine for septic shock",
    "p4": "Early vasopressors in septic shock: a randomized trial",
    "p5": "Fluid resuscitation strategies in septic shock",
}
HORS_SUJET = {
    "h1": "Inhaled corticosteroids for pediatric asthma",
    "h2": "Asthma exacerbations in children at the emergency department",
    "h3": "Budesonide maintenance therapy in adult asthma",
}
A_PREDIRE = {
    "x_choc": "Angiotensin II for refractory septic shock",
    "x_asthme": "Montelukast for asthma in children",
    "x_rien": "Hip fracture surgery outcomes",
}


def _documents(titres: dict[str, str]) -> list[tuple[str, str, str | None]]:
    return [(pmid, titre, "Crit Care") for pmid, titre in titres.items()]


CORPUS = _documents(PERTINENTS | HORS_SUJET | A_PREDIRE)
NOTES = dict.fromkeys(PERTINENTS, 3) | dict.fromkeys(HORS_SUJET, 1)


# --- Tokenisation ---------------------------------------------------------------


def test_tokenize_normalise_et_forme_des_bigrammes() -> None:
    termes = tokenize("Choc SEPTIQUE pédiatrique : the patients in 2026")
    assert "pediatrique" in termes  # accents retirés
    assert "choc septique" in termes  # bigramme
    assert "patient" in termes  # pluriel ramené au singulier
    assert "the" not in termes and "in" not in termes  # mots vides, mots courts
    assert "2026" not in termes  # nombres seuls écartés


def test_le_singulier_epargne_sepsis() -> None:
    assert "sepsis" in tokenize("Sepsis")
    assert "stress" in tokenize("Stress")


def test_la_revue_est_un_terme_a_part() -> None:
    assert features("Septic shock", "N Engl J Med")["journal:n engl j med"] == 1


def test_vecteurs_normes() -> None:
    vecteurs = build_vectors(CORPUS)
    for vecteur in vecteurs.values():
        assert sum(w * w for w in vecteur.values()) == pytest.approx(1.0)


# --- Démarrage à froid ------------------------------------------------------------


def test_trop_peu_de_notes() -> None:
    statut = learning_status({str(i): 3 for i in range(MIN_RATINGS - 1)} | {"z": 1})
    assert statut.ready  # 8 notes, un 3, un 1
    statut = learning_status({str(i): 3 for i in range(MIN_RATINGS - 2)} | {"z": 1})
    assert not statut.ready
    assert statut.missing == "1 note(s) de plus"


def test_il_faut_des_deux_cotes() -> None:
    que_des_3 = learning_status({str(i): 3 for i in range(MIN_RATINGS)})
    assert que_des_3.missing == "au moins un article noté 1 ou 2"
    que_des_2 = learning_status({str(i): 2 for i in range(MIN_RATINGS)})
    assert que_des_2.missing == "au moins un article noté 3"


def test_pas_de_prediction_avant_le_seuil() -> None:
    notes = dict(list(NOTES.items())[:5])
    assert predict_unrated(CORPUS, notes) == {}


# --- Prédiction -------------------------------------------------------------------


def test_les_articles_notes_ne_sont_pas_predits() -> None:
    predictions = predict_unrated(CORPUS, NOTES)
    assert set(predictions) == set(A_PREDIRE)


def test_un_voisin_des_pertinents_est_juge_pertinent() -> None:
    predictions = predict_unrated(CORPUS, NOTES)
    assert predictions["x_choc"].score > 2.5
    assert predictions["x_asthme"].score < 2.0
    assert predictions["x_choc"].neighbor in PERTINENTS
    assert predictions["x_asthme"].neighbor in HORS_SUJET


def test_les_scores_restent_sur_l_echelle() -> None:
    for prediction in predict_unrated(CORPUS, NOTES).values():
        assert 1.0 <= prediction.score <= 3.0


def test_sans_voisin_la_prediction_est_ramenee_vers_la_moyenne() -> None:
    """« Hip fracture » ne partage que la revue : il reste près de la moyenne."""
    moyenne = sum(NOTES.values()) / len(NOTES)
    choc = predict_unrated(CORPUS, NOTES)["x_choc"].score
    rien = predict_unrated(CORPUS, NOTES)["x_rien"].score
    assert abs(rien - moyenne) < abs(choc - moyenne)


def test_aucun_terme_commun_donne_exactement_la_moyenne() -> None:
    corpus = [(pmid, titre, None) for pmid, titre, _ in CORPUS]
    prediction = predict_unrated(corpus, NOTES)["x_rien"]
    assert prediction.neighbor is None
    assert prediction.score == pytest.approx(sum(NOTES.values()) / len(NOTES))


def test_prediction_deterministe() -> None:
    assert predict_unrated(CORPUS, NOTES) == predict_unrated(CORPUS, NOTES)


def test_leave_one_out_bat_la_moyenne_sur_un_corpus_separable() -> None:
    evaluation = leave_one_out(CORPUS, NOTES)
    assert evaluation is not None
    assert len(evaluation.pairs) == len(NOTES)
    assert evaluation.mae < evaluation.baseline_mae
    assert evaluation.confusion().get((3, 3), 0) >= 4


def test_leave_one_out_inactif_avant_le_seuil() -> None:
    assert leave_one_out(CORPUS, {"p1": 3, "h1": 1}) is None


# --- Accès base ---------------------------------------------------------------------


@pytest.fixture
def base(tmp_path: Path) -> tuple[Path, int]:
    path = init_db(tmp_path / "veille.db")
    with get_conn(path) as conn:
        query_id = repo.create_query(conn, name="Choc", pubmed_query="shock")
        for pmid, titre, revue in CORPUS:
            repo.upsert_article(
                conn,
                ArticleSummary(
                    pmid=pmid,
                    title=titre,
                    journal=revue,
                    issn=None,
                    eissn=None,
                    pub_date="2026-01-15",
                ),
            )
            repo.link_article(conn, query_id, pmid)
    return path, query_id


def _noter_tout(path: Path, query_id: int) -> None:
    with get_conn(path) as conn:
        for pmid, note in NOTES.items():
            repo.set_rating(conn, query_id, pmid, note)


def _scores(path: Path, query_id: int) -> dict[str, float]:
    with get_conn(path) as conn:
        return {
            row["pmid"]: row["score"]
            for row in conn.execute(
                "SELECT pmid, score FROM relevance_scores WHERE query_id = ?",
                (query_id,),
            )
        }


def test_recompute_ecrit_puis_vide(base: tuple[Path, int]) -> None:
    path, query_id = base
    _noter_tout(path, query_id)
    with get_conn(path) as conn:
        assert recompute_scores(conn, query_id) == len(A_PREDIRE)
    assert set(_scores(path, query_id)) == set(A_PREDIRE)

    # Une note retirée repasse sous le seuil : plus aucune prédiction.
    with get_conn(path) as conn:
        repo.clear_rating(conn, query_id, "p1")
        assert recompute_scores(conn, query_id) == 0
    assert _scores(path, query_id) == {}


def test_la_liste_porte_la_prediction(base: tuple[Path, int]) -> None:
    path, query_id = base
    _noter_tout(path, query_id)
    with get_conn(path) as conn:
        recompute_scores(conn, query_id)
        lignes = {
            row["pmid"]: row for row in repo.list_articles_for_query(conn, query_id)
        }
    assert lignes["x_choc"]["predicted"] > 2.5
    assert lignes["p1"]["predicted"] is None  # noté : pas de prédiction
    assert lignes["p1"]["rating"] == 3


def test_explain_nomme_le_voisin(base: tuple[Path, int]) -> None:
    path, query_id = base
    _noter_tout(path, query_id)
    with get_conn(path) as conn:
        explication = explain(conn, query_id, "x_choc")
        assert explain(conn, query_id, "p1") is None  # article noté
    assert explication is not None
    assert explication.neighbor_rating == 3
    assert explication.neighbor_title in PERTINENTS.values()


def test_drapeau_a_zero_ne_calcule_rien(
    base: tuple[Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, query_id = base
    _noter_tout(path, query_id)
    monkeypatch.setattr(
        db, "settings", dataclasses.replace(db.settings, enable_relevance=False)
    )
    with get_conn(path) as conn:
        assert recompute_scores(conn, query_id) == 0
        assert explain(conn, query_id, "x_choc") is None
    assert _scores(path, query_id) == {}


class _PubMedFige:
    """Renvoie toujours un nouvel article sur le choc septique."""

    async def esearch(self, term: str, *, reldate_days: int | None = None):
        return ESearchResult(["x_neuf"], 1, term, [], [])

    async def esummary(self, pmids: list[str]) -> list[ArticleSummary]:
        return [
            ArticleSummary(
                pmid="x_neuf",
                title="Hydrocortisone in septic shock",
                journal="Crit Care",
                issn=None,
                eissn=None,
                pub_date="2026-01-15",
            )
        ]


def test_le_rafraichissement_predit_les_nouveaux_venus(
    base: tuple[Path, int],
) -> None:
    """Étape 7 : l'article rattaché par le refresh reçoit aussitôt sa prédiction."""
    path, query_id = base
    _noter_tout(path, query_id)
    with get_conn(path) as conn:
        query = repo.get_query(conn, query_id)
        assert query is not None
        asyncio.run(refresh_query(conn, query, pubmed=_PubMedFige()))  # type: ignore[arg-type]
    assert _scores(path, query_id)["x_neuf"] > 2.5
