"""Tests de lecture des réponses E-utilities, sur charges utiles figées."""

import pytest

from app.pubmed import parse_esearch, parse_esummary_entry, parse_pub_date

# Entrée ESummary réelle (structure allégée aux champs utilisés).
NEJM_ENTRY = {
    "uid": "39energy",
    "title": "Semaglutide in Patients with Heart Failure.",
    "source": "N Engl J Med",
    "fulljournalname": "The New England journal of medicine",
    "issn": "0028-4793",
    "essn": "1533-4406",
    "pubdate": "2024 Jan 15",
    "sortpubdate": "2024/01/15 00:00",
}


def test_parse_esummary_entry_extrait_les_deux_issn() -> None:
    summary = parse_esummary_entry(NEJM_ENTRY)
    assert summary is not None
    assert summary.issn == "00284793"
    assert summary.eissn == "15334406"
    # Les deux servent à la jointure qualité, dans cet ordre.
    assert summary.issn_candidates() == ["00284793", "15334406"]


def test_parse_esummary_entry_prend_l_abreviation_nlm() -> None:
    summary = parse_esummary_entry(NEJM_ENTRY)
    assert summary is not None
    assert summary.journal == "N Engl J Med"
    assert summary.url == "https://pubmed.ncbi.nlm.nih.gov/39energy/"


def test_parse_esummary_entry_en_erreur() -> None:
    assert parse_esummary_entry({"uid": "1", "error": "cannot get document summary"}) is None
    assert parse_esummary_entry({}) is None


def test_parse_esummary_entry_sans_issn() -> None:
    summary = parse_esummary_entry({"uid": "42", "title": "T", "source": "Rev"})
    assert summary is not None
    assert summary.issn_candidates() == []


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"sortpubdate": "2024/01/15 00:00"}, "2024-01-15"),
        ({"pubdate": "2024 Jan 15"}, "2024-01-15"),
        ({"pubdate": "2024 Mar"}, "2024-03"),
        ({"pubdate": "2024"}, "2024"),
        ({"pubdate": "2024 Spring"}, "2024"),  # saison : on garde l'année
        ({}, None),
    ],
)
def test_parse_pub_date(entry: dict, expected: str | None) -> None:
    assert parse_pub_date(entry) == expected


def test_parse_pub_date_prefere_sortpubdate() -> None:
    assert parse_pub_date({"sortpubdate": "2024/01/15 00:00", "pubdate": "2024"}) == "2024-01-15"


def test_parse_esearch_nominal() -> None:
    payload = {
        "esearchresult": {
            "count": "1234",
            "idlist": ["39000001", "39000002"],
            "querytranslation": '"N Engl J Med"[Journal] AND randomized[pt]',
            "warninglist": {"phrasesignored": [], "quotedphrasesnotfound": [], "outputmessages": []},
            "errorlist": {"phrasesnotfound": [], "fieldsnotfound": []},
        }
    }
    result = parse_esearch(payload)
    assert result.count == 1234
    assert result.pmids == ["39000001", "39000002"]
    assert "N Engl J Med" in result.translation
    assert result.warnings == []
    assert result.errors == []


def test_parse_esearch_remonte_les_avertissements_de_pubmed() -> None:
    # C'est notre seule validation de syntaxe : on rapporte, on n'interprète pas.
    payload = {
        "esearchresult": {
            "count": "0",
            "idlist": [],
            "querytranslation": "blabla[All Fields]",
            "warninglist": {"quotedphrasesnotfound": ["\"terme inexistant\""], "outputmessages": []},
            "errorlist": {"fieldsnotfound": ["[tia]"], "phrasesnotfound": []},
        }
    }
    result = parse_esearch(payload)
    assert result.count == 0
    assert any("terme inexistant" in w for w in result.warnings)
    assert any("[tia]" in e for e in result.errors)


def test_parse_esearch_vide() -> None:
    result = parse_esearch({})
    assert result.count == 0 and result.pmids == []
