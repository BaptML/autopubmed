"""Tests du rendu bibliographique : Vancouver et BibTeX."""

import pytest

from app.citation import bibtex_key, to_bibliography, to_bibtex, to_vancouver

COMPLET = {
    "pmid": "40000001",
    "title": "Semaglutide in Patients with Heart Failure",
    "journal": "N Engl J Med",
    "journal_full": "The New England journal of medicine",
    "pub_date": "2026-08-13",
    "volume": "390",
    "issue": "2",
    "pages": "128-139",
    "doi": "10.1056/NEJMoa2306963",
    "authors": "Kosiborod MN; Abildstrom SZ",
}


def test_vancouver_reference_complete() -> None:
    assert to_vancouver(COMPLET) == (
        "Kosiborod MN, Abildstrom SZ. "
        "Semaglutide in Patients with Heart Failure. "
        "N Engl J Med. 2026;390(2):128-139. "
        "doi:10.1056/NEJMoa2306963. PMID: 40000001."
    )


def test_vancouver_coupe_au_dela_de_six_auteurs() -> None:
    article = dict(COMPLET, authors="; ".join(f"Auteur {i}" for i in "ABCDEFGH"))
    rendu = to_vancouver(article)
    assert "Auteur F, et al." in rendu
    assert "Auteur G" not in rendu


def test_vancouver_sans_auteur_ni_pagination() -> None:
    """Éditoriaux et articles en ligne d'abord : on affiche ce qu'on a."""
    article = {
        "pmid": "1",
        "title": "Un éditorial",
        "journal": "Rev",
        "pub_date": "2026",
        "authors": "",
        "volume": "",
        "issue": "",
        "pages": "",
        "doi": "",
    }
    assert to_vancouver(article) == "Un éditorial. Rev. 2026. PMID: 1."


def test_vancouver_pages_sans_volume() -> None:
    article = dict(COMPLET, volume="", issue="", pages="e12345")
    assert "2026;e12345." in to_vancouver(article)


def test_bibliographie_numerotee() -> None:
    rendu = to_bibliography([COMPLET, dict(COMPLET, pmid="2")])
    lignes = rendu.splitlines()
    assert len(lignes) == 2
    assert lignes[0].startswith("1. Kosiborod MN")
    assert lignes[1].startswith("2. Kosiborod MN")


def test_bibliographie_vide() -> None:
    assert to_bibliography([]) == ""


def test_bibtex_entree_complete() -> None:
    rendu = to_bibtex([COMPLET])
    assert rendu.startswith("@article{kosiborod2026semaglutide,")
    assert "author  = {Kosiborod, MN and Abildstrom, SZ}" in rendu
    # Double accolade : la casse du titre est protégée à la compilation.
    assert "title   = {{Semaglutide in Patients with Heart Failure}}" in rendu
    assert "journal = {The New England journal of medicine}" in rendu
    assert "pages   = {128--139}" in rendu  # tiret demi-cadratin BibTeX
    assert "doi     = {10.1056/NEJMoa2306963}" in rendu
    assert "url     = {https://pubmed.ncbi.nlm.nih.gov/40000001/}" in rendu


def test_bibtex_desambiguise_les_cles_identiques() -> None:
    rendu = to_bibtex([COMPLET, dict(COMPLET, pmid="2")])
    assert "@article{kosiborod2026semaglutide," in rendu
    assert "@article{kosiborod2026semaglutidea," in rendu


def test_bibtex_echappe_les_caracteres_speciaux() -> None:
    rendu = to_bibtex([dict(COMPLET, title="Cost & benefit of GLP_1")])
    assert r"Cost \& benefit of GLP\_1" in rendu


def test_bibtex_nom_collectif_reste_entier() -> None:
    rendu = to_bibtex([dict(COMPLET, authors="STEP-HFpEF Trial Committees")])
    assert "author  = {STEP-HFpEF Trial Committees}" in rendu


@pytest.mark.parametrize(
    ("article", "attendu"),
    [
        (
            {"authors": "Doe J", "title": "A study of X", "pub_date": "2026"},
            "doe2026study",
        ),
        ({"authors": "", "title": "Sans auteur", "pub_date": "2026"}, "anon2026sans"),
        ({"authors": "Doe J", "title": "", "pub_date": ""}, "doesdarticle"),
        # Les accents sont translittérés : une clé BibTeX reste en ASCII.
        (
            {"authors": "Müller K", "title": "Étude", "pub_date": "2026"},
            "muller2026etude",
        ),
    ],
)
def test_bibtex_key(article: dict, attendu: str) -> None:
    assert bibtex_key(article) == attendu


def test_bibtex_vide() -> None:
    assert to_bibtex([]) == ""
