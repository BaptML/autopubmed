"""Tests de normalisation ISSN — cas réels tirés de Scimago et d'ESummary."""

import pytest

from app.issn import normalize_issn, split_issns


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0028-4793", "00284793"),  # forme PubMed, avec tiret
        ("00284793", "00284793"),  # forme Scimago, sans tiret
        (" 1533-4406 ", "15334406"),  # espaces parasites
        ("0002-9432", "00029432"),
        ("1476-4687", "14764687"),
        ("0264-410x", "0264410X"),  # clé de contrôle X, en minuscule
    ],
)
def test_normalize_issn_valide(raw: str, expected: str) -> None:
    assert normalize_issn(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "-",  # valeur de remplissage Scimago
        "1234",  # trop court
        "123456789",  # trop long
        "0028-479A",  # lettre interdite en clé de contrôle
        "N Engl J Med",  # un nom de revue, pas un ISSN
    ],
)
def test_normalize_issn_invalide(raw: str | None) -> None:
    assert normalize_issn(raw) is None


def test_split_issns_cellule_scimago_du_nejm() -> None:
    # Cas exact du CSV Scimago 2025 : papier et électronique dans une cellule.
    assert split_issns("00284793, 15334406") == ["00284793", "15334406"]


def test_split_issns_un_seul_issn() -> None:
    assert split_issns("00029629") == ["00029629"]


def test_split_issns_dedoublonne_en_preservant_l_ordre() -> None:
    assert split_issns("1533-4406, 15334406, 0028-4793") == ["15334406", "00284793"]


def test_split_issns_ignore_les_fragments_invalides() -> None:
    assert split_issns("00284793, -, ") == ["00284793"]


def test_split_issns_vide() -> None:
    assert split_issns(None) == []
    assert split_issns("-") == []
