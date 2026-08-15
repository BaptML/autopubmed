"""Couche de filtrage qualité.

Appliquée **après** récupération, à partir de champs séparés de la requête.
Rien de tout cela n'est jamais injecté dans la chaîne PubMed.
"""

from __future__ import annotations

from app.models import JournalQuality


def passes_quality(
    quality: JournalQuality | None,
    quartile_min: int | None,
    citedness_min: float | None,
) -> bool:
    """Décide si un article est retenu, d'après la qualité de sa revue.

    - `quartile_min = 2` garde Q1 et Q2 (le quartile stocké est meilleur quand
      il est plus petit).
    - Un filtre à None ne s'applique pas.
    - Revue non classée ou inconnue : écartée dès qu'un filtre est actif
      (choix conservateur du §7 — mieux vaut manquer un article que polluer
      la veille avec une revue dont la qualité n'est pas établie).
    """
    if quartile_min is None and citedness_min is None:
        return True

    if quartile_min is not None:
        if quality is None or quality.quartile is None:
            return False
        if quality.quartile > quartile_min:
            return False

    if citedness_min is not None:
        if quality is None or quality.citedness is None:
            return False
        if quality.citedness < citedness_min:
            return False

    return True
