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
class SavedQuery:
    """Requête sauvegardée, telle que stockée en base."""

    id: int
    name: str
    folder: str
    pubmed_query: str
    quartile_min: int | None
    citedness_min: float | None
    sort: str
    created_at: str
    last_refreshed_at: str | None
