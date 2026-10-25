"""Suggestions de termes pour la chaîne PubMed, tirées des notes 3 et 1."""

from app.relevance import suggest_terms

PERTINENTS = {
    "p1": "Septic shock and vasopressor timing",
    "p2": "Norepinephrine dosing in septic shock",
    "p3": "Vasopressin in septic shock with diabetes",
    "p4": "Early vasopressors in septic shock",
}
HORS_SUJET = {
    "h1": "Inhaled corticosteroids for pediatric asthma",
    "h2": "Asthma exacerbations in children with diabetes",
    "h3": "Budesonide in adult asthma",
}


def _suggerer(notes: dict[str, int], titres: dict[str, str] | None = None):
    titres = titres or (PERTINENTS | HORS_SUJET | {"s1": "Shock in asthma"})
    documents = [(pmid, titre, "Crit Care") for pmid, titre in titres.items()]
    return suggest_terms(documents, notes)


NOTES = dict.fromkeys(PERTINENTS, 3) | dict.fromkeys(HORS_SUJET, 1)


def _termes(liste) -> list[str]:
    return [s.term for s in liste]


def test_termes_attendus_de_chaque_cote() -> None:
    suggestions = _suggerer(NOTES)
    assert (suggestions.relevant_titles, suggestions.irrelevant_titles) == (4, 3)
    assert _termes(suggestions.include)[0] == "septic shock"
    assert _termes(suggestions.exclude) == ["asthma"]
    choc = suggestions.include[0]
    assert (choc.relevant, choc.irrelevant) == (4, 0)
    assert choc.fragment == 'AND "septic shock"[tiab]'
    assert suggestions.exclude[0].fragment == 'NOT "asthma"[tiab]'


def test_un_mot_toujours_dans_un_bigramme_n_est_pas_repete() -> None:
    termes = _termes(_suggerer(NOTES).include)
    assert "septic" not in termes
    assert "shock" not in termes


def test_terme_present_des_deux_cotes_ou_isole_ecarte() -> None:
    suggestions = _suggerer(NOTES)
    tous = _termes(suggestions.include + suggestions.exclude)
    assert "diabetes" not in tous  # un titre de chaque côté : rien ne distingue
    assert "budesonide" not in tous  # un seul titre : coïncidence


def test_formes_regroupees_et_phrases_contigues() -> None:
    suggestions = _suggerer(NOTES)
    # « vasopressor » et « vasopressors » comptent ensemble : deux titres.
    vaso = next(s for s in suggestions.include if s.term == "vasopressor")
    assert vaso.relevant == 2
    tous = _termes(suggestions.include + suggestions.exclude)
    # « shock and vasopressor » : les deux mots ne se suivent pas dans le titre.
    assert "shock vasopressor" not in tous


def test_le_pluriel_reste_ecrit_comme_dans_les_titres() -> None:
    titres = {
        "p1": "Diabetes and sepsis outcomes",
        "p2": "Diabetes in sepsis survivors",
        "h1": "Knee arthroplasty",
        "h2": "Hip arthroplasty",
    }
    notes = {"p1": 3, "p2": 3, "h1": 1, "h2": 1}
    suggestions = _suggerer(notes, titres)
    assert "diabetes" in _termes(suggestions.include)
    assert "diabete" not in _termes(suggestions.include)


def test_les_notes_2_n_entrent_pas() -> None:
    notes = NOTES | {"s1": 2}
    assert _suggerer(notes) == _suggerer(NOTES)


def test_rien_sans_les_deux_groupes() -> None:
    suggestions = _suggerer(dict.fromkeys(PERTINENTS, 3))
    assert suggestions.include == suggestions.exclude == []
    assert suggestions.irrelevant_titles == 0
    assert _suggerer({}).include == []
