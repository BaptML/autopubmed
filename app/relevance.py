"""Pertinence prédite d'après les notes de 1 à 3 de l'utilisateur, requête par requête.

Plus proches voisins sur une représentation TF-IDF du titre et de la revue :
un article non noté reçoit la moyenne des notes de ses voisins notés, pondérée
par la similarité et tirée vers la note moyenne de la requête quand les voisins
sont lointains. Aucune dépendance, et chaque prédiction s'explique par un
voisin réel.

Le résumé n'entre pas dans la représentation : il n'est en cache que pour les
articles ouverts (efetch à la demande), et la comparaison entre notés — presque
tous ouverts — et non notés serait faussée.

Usage :
  uv run python -m app.relevance --id N   prédictions et évaluation d'une requête
  uv run python -m app.relevance --all    recalcul de toutes les requêtes
"""

from __future__ import annotations

import argparse
import heapq
import math
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise

from app import repo
from app.db import get_conn, init_db, relevance_enabled

# Voisins retenus pour une prédiction.
K_NEIGHBORS = 10
# Poids de la note moyenne de la requête, en « similarité équivalente » : un
# voisin unique à 0,1 de similarité ne suffit pas à décider d'un 3 ou d'un 1.
SHRINKAGE = 0.5
# Démarrage à froid : en deçà, aucune prédiction n'est produite.
MIN_RATINGS = 8

# Mots grammaticaux anglais de plus de deux lettres (les plus courts sont
# écartés d'office). Les termes fréquents mais porteurs de sens restent :
# l'IDF les atténue déjà.
# Bloc de texte plutôt que liste : plus lisible pour une soixantaine de mots.
_STOPWORDS = frozenset(
    """
    the and for with without from into onto among after before during between
    within than that this these those their there its are was were been being
    has have had not but can could may might does did how what which who whom
    why when where our your his her they them versus via per about against
    over under upon also more most less both each other such then thus
    """.split()  # noqa: SIM905
)
_WORD = re.compile(r"[a-z0-9]+")


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _singular(word: str) -> str:
    """Pluriel anglais régulier ramené au singulier : « patients » → « patient ».

    Volontairement grossier : la même règle s'appliquant partout, une forme
    tronquée à tort (« diabetes » → « diabete ») reste cohérente.
    """
    if len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _words(text: str) -> list[tuple[int, str, str]]:
    """(position dans le texte, forme normalisée, forme d'origine) des mots retenus.

    Les nombres seuls (années, effectifs) sont écartés.
    """
    return [
        (i, _singular(w), w)
        for i, w in enumerate(_WORD.findall(_strip_accents(text.lower())))
        if len(w) > 2 and not w.isdigit() and w not in _STOPWORDS
    ]


def tokenize(text: str) -> list[str]:
    """Unigrammes puis bigrammes d'un texte, normalisés.

    Les bigrammes captent les expressions (« septic shock ») que deux mots
    isolés diluent.
    """
    words = [norm for _, norm, _ in _words(text)]
    return words + [f"{a} {b}" for a, b in pairwise(words)]


def features(title: str, journal: str | None) -> Counter[str]:
    """Termes d'un article : son titre, plus sa revue comme terme à part."""
    terms = Counter(tokenize(title))
    if journal:
        terms[f"journal:{_strip_accents(journal.lower()).strip()}"] += 1
    return terms


Vector = dict[str, float]


def build_vectors(
    documents: Iterable[tuple[str, str, str | None]],
) -> dict[str, Vector]:
    """Vecteurs TF-IDF normés, par PMID. L'IDF est propre au corpus fourni."""
    counts = {pmid: features(title, journal) for pmid, title, journal in documents}
    total = len(counts)
    df: Counter[str] = Counter()
    for terms in counts.values():
        df.update(terms.keys())
    idf = {term: math.log((1 + total) / (1 + n)) + 1 for term, n in df.items()}

    vectors: dict[str, Vector] = {}
    for pmid, terms in counts.items():
        weights = {t: (1 + math.log(n)) * idf[t] for t, n in terms.items()}
        norm = math.sqrt(sum(w * w for w in weights.values()))
        vectors[pmid] = {t: w / norm for t, w in weights.items()} if norm else {}
    return vectors


# --- Démarrage à froid --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LearningStatus:
    """Où en est l'apprentissage d'une requête, pour l'afficher."""

    count: int
    needed: int
    has_relevant: bool  # au moins un article noté 3
    has_irrelevant: bool  # au moins un article noté 1 ou 2

    @property
    def ready(self) -> bool:
        return self.count >= self.needed and self.has_relevant and self.has_irrelevant

    @property
    def missing(self) -> str | None:
        """Ce qui manque encore, en clair. None si le modèle est prêt."""
        if self.count < self.needed:
            return f"{self.needed - self.count} note(s) de plus"
        if not self.has_relevant:
            return "au moins un article noté 3"
        if not self.has_irrelevant:
            return "au moins un article noté 1 ou 2"
        return None


def learning_status(ratings: dict[str, int]) -> LearningStatus:
    notes = ratings.values()
    return LearningStatus(
        count=len(ratings),
        needed=MIN_RATINGS,
        has_relevant=any(n == 3 for n in notes),
        has_irrelevant=any(n <= 2 for n in notes),
    )


# --- Prédiction ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Prediction:
    score: float  # sur l'échelle de 1 à 3
    neighbor: str | None  # PMID noté le plus proche ; None si aucun terme commun
    similarity: float


class Model:
    """Articles notés d'une requête, indexés par terme.

    L'index inversé ne compare un article qu'aux notés qui partagent au moins
    un terme avec lui : avec des titres d'une quinzaine de termes, c'est une
    fraction des paires.
    """

    def __init__(self, vectors: dict[str, Vector], ratings: dict[str, int]) -> None:
        self.ratings = {p: r for p, r in ratings.items() if p in vectors}
        self._postings: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for pmid in self.ratings:
            for term, weight in vectors[pmid].items():
                self._postings[term].append((pmid, weight))

    def predict(self, vector: Vector, exclude: str | None = None) -> Prediction:
        """Note attendue d'un vecteur. `exclude` sert au leave-one-out."""
        similarities: dict[str, float] = defaultdict(float)
        for term, weight in vector.items():
            for pmid, other in self._postings.get(term, ()):
                if pmid != exclude:
                    similarities[pmid] += weight * other
        # Égalités départagées par le PMID : même entrée, même sortie.
        neighbors = heapq.nlargest(
            K_NEIGHBORS, similarities.items(), key=lambda item: (item[1], item[0])
        )

        notes = [r for p, r in self.ratings.items() if p != exclude]
        mean = sum(notes) / len(notes)
        numerator = sum(sim * self.ratings[p] for p, sim in neighbors)
        denominator = sum(sim for _, sim in neighbors)
        score = (numerator + SHRINKAGE * mean) / (denominator + SHRINKAGE)
        best, best_sim = neighbors[0] if neighbors else (None, 0.0)
        return Prediction(score, best, best_sim)


def predict_unrated(
    documents: list[tuple[str, str, str | None]], ratings: dict[str, int]
) -> dict[str, Prediction]:
    """Prédictions des articles non notés. Vide tant que le modèle n'est pas prêt.

    Un article noté n'est pas prédit : sa note fait foi.
    """
    vectors = build_vectors(documents)
    ratings = {p: r for p, r in ratings.items() if p in vectors}
    if not learning_status(ratings).ready:
        return {}
    model = Model(vectors, ratings)
    return {
        pmid: model.predict(vector)
        for pmid, vector in vectors.items()
        if pmid not in ratings
    }


# --- Évaluation ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Evaluation:
    """Leave-one-out : chaque note est prédite à partir des autres."""

    pairs: list[tuple[int, float]]  # (note réelle, note prédite)
    baseline_mae: float  # en prédisant simplement la moyenne des autres notes

    @property
    def mae(self) -> float:
        return sum(abs(r - p) for r, p in self.pairs) / len(self.pairs)

    def confusion(self) -> dict[tuple[int, int], int]:
        """(réelle, prédite arrondie) → effectif."""
        table: Counter[tuple[int, int]] = Counter()
        for real, predicted in self.pairs:
            table[(real, min(3, max(1, round(predicted))))] += 1
        return dict(table)


def leave_one_out(
    documents: list[tuple[str, str, str | None]], ratings: dict[str, int]
) -> Evaluation | None:
    """Évalue le modèle sur les notes existantes. None s'il n'est pas prêt."""
    vectors = build_vectors(documents)
    ratings = {p: r for p, r in ratings.items() if p in vectors}
    if not learning_status(ratings).ready:
        return None
    model = Model(vectors, ratings)
    pairs = [
        (r, model.predict(vectors[p], exclude=p).score) for p, r in ratings.items()
    ]
    total = sum(ratings.values())
    count = len(ratings)
    baseline = [abs(r - (total - r) / (count - 1)) for r in ratings.values()]
    return Evaluation(pairs, sum(baseline) / count)


# --- Suggestions pour la chaîne PubMed ------------------------------------------

# Un terme n'est proposé que s'il revient dans au moins autant de titres du
# groupe où il est suggéré : un mot vu une fois n'est qu'une coïncidence.
MIN_TERM_TITLES = 2
SUGGESTIONS_PER_SIDE = 8


@dataclass(frozen=True, slots=True)
class TermSuggestion:
    term: str  # tel qu'écrit dans les titres, pluriel compris
    relevant: int  # titres notés 3 qui le contiennent
    irrelevant: int  # titres notés 1 qui le contiennent
    log_odds: float  # > 0 : côté pertinent

    @property
    def fragment(self) -> str:
        """Fragment prêt à coller au bout de la chaîne. Jamais inséré d'office."""
        operator = "AND" if self.log_odds > 0 else "NOT"
        return f'{operator} "{self.term}"[tiab]'


@dataclass(frozen=True, slots=True)
class TermSuggestions:
    relevant_titles: int
    irrelevant_titles: int
    include: list[TermSuggestion]
    exclude: list[TermSuggestion]


def _title_terms(title: str) -> dict[str, str]:
    """Termes distincts d'un titre → leur forme d'origine.

    Bigrammes seulement entre mots contigus : proposé entre guillemets, le
    terme doit se retrouver tel quel en phrase dans PubMed, ce que « shock
    and vasopressor » → « shock vasopressor » ne permettrait pas.
    """
    words = _words(title)
    terms = {norm: surface for _, norm, surface in words}
    for (i, a, surface_a), (j, b, surface_b) in pairwise(words):
        if j == i + 1:
            terms[f"{a} {b}"] = f"{surface_a} {surface_b}"
    return terms


def _log_odds(
    relevant: int, relevant_total: int, irrelevant: int, irrelevant_total: int
) -> float:
    """Log-odds ratio de présence du terme, 3 contre 1, lissé d'un demi.

    Le lissage évite l'infini d'un terme absent d'un côté et tempère les
    petits effectifs.
    """
    return math.log(
        (relevant + 0.5)
        * (irrelevant_total - irrelevant + 0.5)
        / ((relevant_total - relevant + 0.5) * (irrelevant + 0.5))
    )


def _without_redundant_words(candidates: list[TermSuggestion]) -> list[TermSuggestion]:
    """Retire un mot isolé qui n'apparaît jamais hors d'un bigramme candidat.

    Mêmes effectifs que « septic shock » : « septic » seul n'apprend rien de plus.
    """
    bigrams = {
        (c.relevant, c.irrelevant, w)
        for c in candidates
        if " " in c.term
        for w in c.term.split()
    }
    return [
        c
        for c in candidates
        if " " in c.term or (c.relevant, c.irrelevant, c.term) not in bigrams
    ]


def suggest_terms(
    documents: list[tuple[str, str, str | None]],
    ratings: dict[str, int],
    limit: int = SUGGESTIONS_PER_SIDE,
) -> TermSuggestions:
    """Termes qui distinguent les titres notés 3 des titres notés 1.

    Les notes 2 n'entrent pas dans la comparaison : un article secondaire ne
    dit ni ce qu'il faut garder, ni ce qu'il faut écarter.
    """
    titles = {pmid: title for pmid, title, _ in documents}
    totals: Counter[int] = Counter()
    present: dict[int, Counter[str]] = {3: Counter(), 1: Counter()}
    surfaces: dict[str, Counter[str]] = defaultdict(Counter)
    for pmid, rating in ratings.items():
        if rating not in present or pmid not in titles:
            continue
        totals[rating] += 1
        for term, surface in _title_terms(titles[pmid]).items():
            present[rating][term] += 1
            surfaces[term][surface] += 1

    if not totals[3] or not totals[1]:
        return TermSuggestions(totals[3], totals[1], [], [])

    candidates = [
        TermSuggestion(
            # Forme la plus fréquente, puis l'ordre alphabétique pour départager.
            term=min(surfaces[term].items(), key=lambda item: (-item[1], item[0]))[0],
            relevant=present[3][term],
            irrelevant=present[1][term],
            log_odds=_log_odds(
                present[3][term], totals[3], present[1][term], totals[1]
            ),
        )
        for term in present[3].keys() | present[1].keys()
    ]
    candidates = _without_redundant_words(candidates)
    candidates.sort(key=lambda c: (-abs(c.log_odds), c.term))
    include = [
        c for c in candidates if c.log_odds > 0 and c.relevant >= MIN_TERM_TITLES
    ]
    exclude = [
        c for c in candidates if c.log_odds < 0 and c.irrelevant >= MIN_TERM_TITLES
    ]
    return TermSuggestions(totals[3], totals[1], include[:limit], exclude[:limit])


# --- Accès base ----------------------------------------------------------------


def recompute_scores(conn: sqlite3.Connection, query_id: int) -> int:
    """Recalcule les prédictions d'une requête. Renvoie le nombre écrit.

    Appelé après chaque note et à la fin de chaque rafraîchissement. Modèle pas
    encore prêt : la table est vidée pour cette requête, rien n'est prédit.
    """
    if not relevance_enabled():
        return 0
    predictions = predict_unrated(
        repo.query_documents(conn, query_id), repo.ratings_for_query(conn, query_id)
    )
    repo.replace_scores(conn, query_id, {p: pr.score for p, pr in predictions.items()})
    return len(predictions)


def suggestions_for_query(
    conn: sqlite3.Connection, query_id: int
) -> TermSuggestions | None:
    """Termes à proposer pour affiner la chaîne. None si la couche est éteinte.

    Lecture seule : la chaîne enregistrée n'est jamais touchée.
    """
    if not relevance_enabled():
        return None
    return suggest_terms(
        repo.query_documents(conn, query_id), repo.ratings_for_query(conn, query_id)
    )


@dataclass(frozen=True, slots=True)
class Explanation:
    score: float
    neighbor_title: str | None
    neighbor_rating: int | None
    similarity: float


def explain(conn: sqlite3.Connection, query_id: int, pmid: str) -> Explanation | None:
    """Pourquoi cette prédiction : le voisin noté le plus proche.

    None si la couche est désactivée, l'article noté ou le modèle pas prêt.
    """
    if not relevance_enabled():
        return None
    ratings = repo.ratings_for_query(conn, query_id)
    if pmid in ratings:
        return None
    documents = repo.query_documents(conn, query_id)
    vectors = build_vectors(documents)
    if pmid not in vectors:
        return None
    ratings = {p: r for p, r in ratings.items() if p in vectors}
    if not learning_status(ratings).ready:
        return None
    prediction = Model(vectors, ratings).predict(vectors[pmid])
    titles = {p: title for p, title, _ in documents}
    return Explanation(
        score=prediction.score,
        neighbor_title=titles.get(prediction.neighbor) if prediction.neighbor else None,
        neighbor_rating=ratings.get(prediction.neighbor)
        if prediction.neighbor
        else None,
        similarity=prediction.similarity,
    )


# --- Ligne de commande ---------------------------------------------------------


def _report(conn: sqlite3.Connection, query_id: int, show: int) -> None:
    query = repo.get_query(conn, query_id)
    if query is None:
        print(f"Requête introuvable : {query_id}")
        return
    documents = repo.query_documents(conn, query_id)
    ratings = repo.ratings_for_query(conn, query_id)
    status = learning_status(ratings)
    written = recompute_scores(conn, query_id)

    print(f"Requête     : {query.name} ({len(documents)} articles)")
    notes = Counter(ratings.values())
    print(
        f"Notes       : {status.count} (3 : {notes[3]} · 2 : {notes[2]} · 1 : {notes[1]})"
    )
    if not status.ready:
        print(f"Modèle      : en apprentissage — il manque {status.missing}.")
        return
    print(f"Prédictions : {written} articles non notés")

    evaluation = leave_one_out(documents, ratings)
    assert evaluation is not None
    print("\nÉvaluation leave-one-out (chaque note prédite sans elle) :")
    print(f"  erreur absolue moyenne : {evaluation.mae:.2f}")
    print(f"  référence (moyenne seule) : {evaluation.baseline_mae:.2f}")
    print("  réelle \\ prédite   1    2    3")
    table = evaluation.confusion()
    for real in (1, 2, 3):
        cells = "".join(f"{table.get((real, p), 0):5d}" for p in (1, 2, 3))
        print(f"  {real}               {cells}")

    predictions = predict_unrated(documents, ratings)
    ranked = sorted(predictions.items(), key=lambda item: -item[1].score)
    titles = {p: title for p, title, _ in documents}
    print(f"\nLes {show} plus pertinents prédits :")
    for pmid, prediction in ranked[:show]:
        print(f"  {prediction.score:.2f}  {titles[pmid][:100]}")
    print(f"\nLes {show} moins pertinents prédits :")
    for pmid, prediction in ranked[-show:]:
        print(f"  {prediction.score:.2f}  {titles[pmid][:100]}")

    suggestions = suggest_terms(documents, ratings)
    print(
        f"\nTermes distinctifs ({suggestions.relevant_titles} titres notés 3, "
        f"{suggestions.irrelevant_titles} notés 1) :"
    )
    for suggestion in suggestions.include + suggestions.exclude:
        print(
            f"  {suggestion.fragment:<40} {suggestion.relevant:>3} / "
            f"{suggestion.irrelevant:>3}   log-odds {suggestion.log_odds:+.2f}"
        )


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Pertinence prédite par requête")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--id", type=int, help="prédire et évaluer la requête N")
    action.add_argument("--all", action="store_true", help="tout recalculer")
    parser.add_argument("--show", type=int, default=10, help="articles listés")
    args = parser.parse_args(argv)

    if not relevance_enabled():
        print("Couche pertinence désactivée (ENABLE_RELEVANCE=0).")
        return 1
    init_db()
    with get_conn() as conn:
        if args.all:
            for query in repo.list_queries(conn):
                written = recompute_scores(conn, query.id)
                print(f"{query.name} : {written} prédiction(s)")
            return 0
        _report(conn, args.id, args.show)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
