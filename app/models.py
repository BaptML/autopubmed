"""Structures de données partagées entre le pipeline et les dépôts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ArticleSummary:
    """Métadonnées légères d'un article, issues d'ESummary (sans résumé)."""

    pmid: str
    title: str
    journal: str | None
    issn: str | None  # normalisé, ISSN papier
    eissn: str | None  # normalisé, ISSN électronique
    pub_date: str | None  # ISO 8601
    # Ajouté après coup, d'où la valeur par défaut : les articles déjà en base
    # n'ont pas de DOI tant qu'ils n'ont pas été réinterrogés.
    doi: str | None = None
    # Métadonnées de citation, remplies seulement pour les favoris.
    authors: str | None = None  # noms joints par « ; », dans l'ordre ESummary
    journal_full: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None

    @property
    def url(self) -> str:
        return f"https://pubmed.ncbi.nlm.nih.gov/{self.pmid}/"

    def issn_candidates(self) -> list[str]:
        """ISSN à essayer pour la jointure qualité, sans doublon."""
        return list(dict.fromkeys(i for i in (self.issn, self.eissn) if i))


@dataclass(frozen=True, slots=True)
class JournalQuality:
    """Verdict qualité d'une revue. Les deux champs peuvent être inconnus."""

    quartile: int | None = None
    citedness: float | None = None


@dataclass(frozen=True, slots=True)
class ESearchResult:
    """Réponse d'ESearch. `translation` et `warnings` viennent de PubMed :
    c'est notre seule validation de la syntaxe (jamais de parsing local)."""

    pmids: list[str]
    count: int
    translation: str
    warnings: list[str]
    errors: list[str]


@dataclass(frozen=True, slots=True)
class JournalMatch:
    """Revue du catalogue NLM. `abbreviation` est ce que PubMed attend en [ta]."""

    title: str
    abbreviation: str | None
    issns: list[str]

    @property
    def pubmed_fragment(self) -> str:
        """Fragment prêt à coller dans une requête."""
        return f'"{self.abbreviation or self.title}"[ta]'


@dataclass(frozen=True, slots=True)
class SavedQuery:
    """Requête sauvegardée, telle que stockée en base."""

    id: int
    name: str
    folder: str
    pubmed_query: str
    quartile_min: int | None
    citedness_min: float | None
    # Ancienneté maximale en années, sur la date de publication. None = illimité.
    max_age_years: int | None
    sort: str
    created_at: str
    last_refreshed_at: str | None
    # Dernière ouverture de la requête, et repère du badge « nouveau » : un
    # article l'est si son `first_seen` est postérieur à `previous_visit_at`.
    last_visited_at: str | None = None
    previous_visit_at: str | None = None
