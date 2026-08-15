"""Tests du filtre qualité — la règle métier la plus facile à casser en silence."""

import pytest

from app.models import JournalQuality
from app.quality import passes_quality

Q1 = JournalQuality(quartile=1, citedness=70.0)
Q2 = JournalQuality(quartile=2, citedness=5.0)
Q4 = JournalQuality(quartile=4, citedness=0.4)
NON_CLASSEE = JournalQuality(quartile=None, citedness=3.0)
INCONNUE = None


def test_sans_filtre_tout_passe() -> None:
    for quality in (Q1, Q4, NON_CLASSEE, INCONNUE):
        assert passes_quality(quality, None, None) is True


@pytest.mark.parametrize(
    ("quality", "quartile_min", "expected"),
    [
        (Q1, 1, True),
        (Q2, 1, False),
        (Q1, 2, True),  # quartile_min=2 garde Q1 et Q2
        (Q2, 2, True),
        (Q4, 2, False),
        (Q4, 4, True),
    ],
)
def test_filtre_quartile(
    quality: JournalQuality, quartile_min: int, expected: bool
) -> None:
    assert passes_quality(quality, quartile_min, None) is expected


def test_revue_non_classee_ecartee_si_filtre_quartile_actif() -> None:
    # §7 : choix conservateur, une revue absente de Scimago ne passe pas.
    assert passes_quality(NON_CLASSEE, 1, None) is False
    assert passes_quality(INCONNUE, 4, None) is False


def test_revue_non_classee_conservee_sans_filtre() -> None:
    assert passes_quality(NON_CLASSEE, None, None) is True


@pytest.mark.parametrize(
    ("quality", "citedness_min", "expected"),
    [
        (Q1, 10.0, True),
        (Q2, 10.0, False),
        (Q2, 5.0, True),  # seuil atteint exactement
        (JournalQuality(quartile=1, citedness=None), 1.0, False),
    ],
)
def test_filtre_citedness(
    quality: JournalQuality, citedness_min: float, expected: bool
) -> None:
    assert passes_quality(quality, None, citedness_min) is expected


def test_les_deux_filtres_se_cumulent() -> None:
    assert passes_quality(Q1, 1, 50.0) is True
    assert passes_quality(Q1, 1, 100.0) is False  # quartile bon, citedness trop faible
    assert passes_quality(Q2, 1, 1.0) is False  # citedness bonne, quartile trop bas
