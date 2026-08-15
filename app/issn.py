"""Normalisation des ISSN.

Point névralgique du projet : PubMed renvoie tantôt l'ISSN papier, tantôt
l'électronique, et Scimago colle les deux dans une seule cellule sans tiret.
Toute jointure article ↔ qualité passe par une forme normalisée unique.
"""

from __future__ import annotations

import re

# Un ISSN normalisé : 7 chiffres + une clé de contrôle (chiffre ou X).
_ISSN_RE = re.compile(r"^\d{7}[\dX]$")

# Scimago sépare ses ISSN par une virgule ; on tolère aussi ';' et l'espace.
_SPLIT_RE = re.compile(r"[,;\s]+")


def normalize_issn(raw: str | None) -> str | None:
    """Renvoie l'ISSN sans tiret ni espace, en majuscules, ou None si invalide.

    Les valeurs de remplissage de Scimago ('-', '') donnent None.
    """
    if not raw:
        return None
    candidate = raw.replace("-", "").replace(" ", "").strip().upper()
    if not _ISSN_RE.match(candidate):
        return None
    return candidate


def split_issns(raw: str | None) -> list[str]:
    """Découpe une cellule pouvant contenir plusieurs ISSN et les normalise.

    Dédoublonne en conservant l'ordre d'apparition. Les fragments invalides
    sont ignorés silencieusement — une revue peut n'avoir qu'un seul ISSN.
    """
    if not raw:
        return []
    seen: dict[str, None] = {}
    for chunk in _SPLIT_RE.split(raw):
        normalized = normalize_issn(chunk)
        if normalized is not None:
            seen.setdefault(normalized, None)
    return list(seen)
