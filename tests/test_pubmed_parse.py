"""Tests de lecture des réponses E-utilities, sur charges utiles figées."""

import pytest

from app.pubmed import (
    parse_abstract_xml,
    parse_authors,
    parse_doi,
    parse_esearch,
    parse_esummary_entry,
    parse_nlmcatalog_xml,
    parse_pub_date,
)

# Extrait réel d'EFetch db=pubmed : résumé structuré, avec balises internes.
ABSTRACT_XML = """<?xml version="1.0"?>
<PubmedArticleSet><PubmedArticle><MedlineCitation><Article>
  <ArticleTitle>Semaglutide in Heart Failure</ArticleTitle>
  <Abstract>
    <AbstractText Label="BACKGROUND" NlmCategory="BACKGROUND">Obesity-related
      HFpEF is common.</AbstractText>
    <AbstractText Label="RESULTS">Weight loss reached 13.3<sup>%</sup> at
      52 weeks.</AbstractText>
  </Abstract>
</Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"""

# Extrait réel d'EFetch db=nlmcatalog : c'est MedlineTA qui porte
# l'abréviation attendue par [ta].
NLMCATALOG_XML = """<?xml version="1.0"?>
<NLMCatalogRecordSet>
  <NLMCatalogRecord>
    <NlmUniqueID>255562</NlmUniqueID>
    <TitleMain><Title>The New England journal of medicine.</Title></TitleMain>
    <MedlineTA>N Engl J Med</MedlineTA>
    <ISSNList>
      <ISSN ValidYN="Y" IssnType="Print">0028-4793</ISSN>
      <ISSN ValidYN="Y" IssnType="Electronic">1533-4406</ISSN>
    </ISSNList>
  </NLMCatalogRecord>
  <NLMCatalogRecord>
    <NlmUniqueID>101760264</NlmUniqueID>
    <TitleMain><Title>NEJM catalyst innovations in care delivery.</Title></TitleMain>
    <MedlineTA>NEJM Catal Innov Care Deliv</MedlineTA>
    <ISSNList><ISSN IssnType="Electronic">2642-0007</ISSN></ISSNList>
  </NLMCatalogRecord>
</NLMCatalogRecordSet>"""

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
    "volume": "390",
    "issue": "2",
    "pages": "128-139",
    "authors": [
        {"name": "Kosiborod MN", "authtype": "Author"},
        {"name": "Abildstrom SZ", "authtype": "Author"},
        {"name": "STEP-HFpEF Trial Committees", "authtype": "CollectiveName"},
    ],
    "elocationid": "doi: 10.1056/NEJMoa2306963",
    "articleids": [
        {"idtype": "pubmed", "value": "38078870"},
        {"idtype": "doi", "value": "10.1056/NEJMoa2306963"},
    ],
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


def test_parse_esummary_entry_extrait_le_doi() -> None:
    summary = parse_esummary_entry(NEJM_ENTRY)
    assert summary is not None
    assert summary.doi == "10.1056/NEJMoa2306963"
    assert summary.doi_url == "https://doi.org/10.1056/NEJMoa2306963"


def test_parse_esummary_entry_extrait_les_metadonnees_de_citation() -> None:
    summary = parse_esummary_entry(NEJM_ENTRY)
    assert summary is not None
    assert summary.authors == (
        "Kosiborod MN; Abildstrom SZ; STEP-HFpEF Trial Committees"
    )
    assert summary.journal_full == "The New England journal of medicine"
    assert summary.volume == "390"
    assert summary.issue == "2"
    assert summary.pages == "128-139"


def test_parse_authors_sans_auteur() -> None:
    # Éditoriaux et errata arrivent sans liste d'auteurs.
    assert parse_authors({"uid": "1"}) is None
    assert parse_authors({"uid": "1", "authors": [{"name": "  "}]}) is None


def test_parse_esummary_entry_sans_metadonnees_de_citation() -> None:
    summary = parse_esummary_entry({"uid": "42", "title": "T", "source": "Rev"})
    assert summary is not None
    assert (summary.authors, summary.volume, summary.issue, summary.pages) == (
        None, None, None, None,
    )


def test_parse_doi_retombe_sur_elocationid() -> None:
    entry = {"uid": "1", "elocationid": "doi: 10.1001/jama.2024.0001"}
    assert parse_doi(entry) == "10.1001/jama.2024.0001"


def test_parse_doi_absent() -> None:
    # `elocationid` porte parfois une pagination ou un pii, jamais un DOI.
    assert parse_doi({"uid": "1", "elocationid": "e12345"}) is None
    assert parse_doi({"uid": "1", "articleids": [{"idtype": "pii", "value": "S01"}]}) is None
    assert parse_doi({}) is None


def test_parse_esummary_entry_sans_doi() -> None:
    summary = parse_esummary_entry({"uid": "42", "title": "T", "source": "Rev"})
    assert summary is not None
    assert summary.doi is None
    assert summary.doi_url is None


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


def test_parse_nlmcatalog_resout_le_piege_nejm() -> None:
    matches = parse_nlmcatalog_xml(NLMCATALOG_XML)
    assert len(matches) == 2

    nejm = matches[0]
    assert nejm.title == "The New England journal of medicine"  # point final retiré
    assert nejm.abbreviation == "N Engl J Med"
    # Le fragment est prêt à coller dans une requête PubMed.
    assert nejm.pubmed_fragment == '"N Engl J Med"[ta]'
    assert nejm.issns == ["00284793", "15334406"]


def test_parse_nlmcatalog_xml_invalide() -> None:
    assert parse_nlmcatalog_xml("pas du xml") == []
    assert parse_nlmcatalog_xml("") == []


def test_parse_abstract_xml_garde_les_sections_et_leurs_etiquettes() -> None:
    sections = parse_abstract_xml(ABSTRACT_XML).split("\n\n")
    assert sections[0] == "BACKGROUND : Obesity-related HFpEF is common."
    # Le texte des balises internes (<sup>) est conservé, sur une seule ligne.
    assert sections[1] == "RESULTS : Weight loss reached 13.3% at 52 weeks."


def test_parse_abstract_xml_sans_etiquette() -> None:
    xml = "<PubmedArticleSet><Abstract><AbstractText>Texte simple.</AbstractText>" \
          "</Abstract></PubmedArticleSet>"
    assert parse_abstract_xml(xml) == "Texte simple."


def test_parse_abstract_xml_sans_resume_ni_xml_valide() -> None:
    # Éditoriaux et lettres n'ont pas de résumé : None, pas une erreur.
    assert parse_abstract_xml("<PubmedArticleSet></PubmedArticleSet>") is None
    assert parse_abstract_xml("pas du xml") is None
