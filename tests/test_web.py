"""Tests des fragments HTML servis à HTMX, sur une base temporaire."""

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import repo
from app.db import connect, get_conn, init_db
from app.main import app, date_fr, get_db
from app.models import ArticleSummary


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    db_path = tmp_path / "web.db"
    init_db(db_path)

    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO journal_quality (issn, journal_name, quartile, source, updated_at)"
            " VALUES ('00284793', 'NEJM', 1, 'scimago', '2026-01-01T00:00:00+00:00')"
        )
        query_id = repo.create_query(
            conn,
            name="NEJM / essais randomisés",
            folder="Références",
            pubmed_query='"N Engl J Med"[ta]',
            quartile_min=1,
        )
        repo.create_query(conn, name="Requête vide", folder="Brouillons", pubmed_query="zzz")
        repo.upsert_article(
            conn,
            ArticleSummary(
                pmid="40000001",
                title="Semaglutide in Heart Failure",
                journal="N Engl J Med",
                issn="00284793",
                eissn=None,
                pub_date="2026-08-13",
                doi="10.1056/NEJMoa2306963",
            ),
        )
        repo.link_article(conn, query_id, "40000001")
        repo.mark_refreshed(conn, query_id)

    def override() -> Iterator:
        # Reproduit fidèlement get_db, commit compris : sans lui, les tests
        # d'écriture passeraient à tort (la réponse relit la même connexion).
        conn = connect(db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as test_client:
        test_client.query_id = query_id  # type: ignore[attr-defined]
        test_client.db_path = db_path  # type: ignore[attr-defined]
        yield test_client
    app.dependency_overrides.clear()


def test_page_d_accueil_est_une_coquille(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    # La liste n'est pas rendue côté serveur : HTMX ira la chercher.
    assert 'hx-get="/queries"' in response.text
    assert "Semaglutide" not in response.text


def test_sidebar_groupe_par_dossier_et_compte_les_non_lus(client: TestClient) -> None:
    response = client.get("/queries")
    assert response.status_code == 200
    assert "Références" in response.text
    assert "Brouillons" in response.text
    assert "NEJM / essais randomisés" in response.text
    # Un article non lu sur la première requête, zéro sur la seconde.
    assert "1 nouv." in response.text
    assert "à jour" in response.text


def test_liste_d_articles_affiche_titre_revue_et_badge(client: TestClient) -> None:
    response = client.get(f"/queries/{client.query_id}/articles")
    assert response.status_code == 200
    assert "Semaglutide in Heart Failure" in response.text
    assert "N Engl J Med" in response.text
    assert 'class="badge q1"' in response.text
    assert "13/08/2026" in response.text
    # Le titre n'est plus un lien : la ligne ouvre l'encart de lecture, d'où
    # part le lien vers PubMed.
    assert (
        f'hx-get="/articles/40000001/panel?query_id={client.query_id}"' in response.text
    )


def test_le_doi_est_un_lien_vers_l_editeur(client: TestClient) -> None:
    lien = 'href="https://doi.org/10.1056/NEJMoa2306963"'
    liste = client.get(f"/queries/{client.query_id}/articles")
    assert lien in liste.text
    # Sans stopPropagation, le clic sur le lien ouvrirait aussi l'encart.
    assert "event.stopPropagation()" in liste.text

    encart = client.get(f"/articles/40000001/panel?query_id={client.query_id}")
    assert lien in encart.text
    assert "10.1056/NEJMoa2306963" in encart.text


def test_article_sans_doi_n_affiche_pas_de_lien(client: TestClient) -> None:
    with get_conn(client.db_path) as conn:
        conn.execute("UPDATE articles SET doi = '' WHERE pmid = '40000001'")
    liste = client.get(f"/queries/{client.query_id}/articles")
    assert "doi.org" not in liste.text


def test_etoiler_depuis_la_liste(client: TestClient) -> None:
    reponse = client.post(
        "/articles/40000001/favorite",
        data={"favorite": 1, "query_id": client.query_id},
    )
    assert reponse.status_code == 200
    assert "★ favori" in reponse.text

    with get_conn(client.db_path) as conn:
        assert repo.count_favorites(conn) == 1

    # Le retrait rend l'étoile creuse et vide la bibliographie.
    retrait = client.post(
        "/articles/40000001/favorite",
        data={"favorite": 0, "query_id": client.query_id},
    )
    assert "☆ favori" in retrait.text
    with get_conn(client.db_path) as conn:
        assert repo.count_favorites(conn) == 0


def test_etoiler_depuis_l_encart_de_lecture(client: TestClient) -> None:
    reponse = client.post(
        "/articles/40000001/favorite",
        data={"favorite": 1, "query_id": client.query_id, "from_panel": 1},
    )
    assert reponse.status_code == 200
    assert "★ Retirer des favoris" in reponse.text


def test_etoiler_un_article_inconnu_renvoie_404(client: TestClient) -> None:
    reponse = client.post(
        "/articles/99999999/favorite",
        data={"favorite": 1, "query_id": client.query_id},
    )
    assert reponse.status_code == 404


def test_filtre_favoris_seulement_sur_une_requete(client: TestClient) -> None:
    tout = client.get(f"/queries/{client.query_id}/articles")
    assert "Semaglutide in Heart Failure" in tout.text

    filtre = client.get(f"/queries/{client.query_id}/articles?favorites=1")
    assert "Semaglutide in Heart Failure" not in filtre.text
    assert "Aucun favori dans cette requête" in filtre.text

    client.post(
        "/articles/40000001/favorite",
        data={"favorite": 1, "query_id": client.query_id},
    )
    filtre = client.get(f"/queries/{client.query_id}/articles?favorites=1")
    assert "Semaglutide in Heart Failure" in filtre.text
    # Le filtre est reconduit par les actions de la barre, sinon il se perdrait.
    assert '"favorites": 1' in filtre.text


def test_liste_vide_explique_comment_la_remplir(client: TestClient) -> None:
    response = client.get("/queries/2/articles")
    assert response.status_code == 200
    assert "Aucun article en cache" in response.text


def test_requete_inexistante_renvoie_404(client: TestClient) -> None:
    assert client.get("/queries/999/articles").status_code == 404


def test_formulaire_de_creation(client: TestClient) -> None:
    response = client.get("/queries/new")
    assert response.status_code == 200
    assert 'hx-post="/queries/new"' in response.text
    # L'aperçu live est câblé avec un anti-rebond, pas à chaque frappe.
    assert 'hx-trigger="keyup changed delay:500ms, load"' in response.text
    assert "Références" in response.text  # dossier existant proposé


def test_creation_conserve_la_chaine_pubmed_a_l_identique(client: TestClient) -> None:
    # Règle absolue : la chaîne brute est la source de vérité, jamais réécrite.
    brute = '  ("heart failure"[MeSH Terms] OR HFpEF[tiab]) AND 2024:2026[dp]  '
    response = client.post(
        "/queries/new",
        data={
            "name": "Nouvelle veille",
            "pubmed_query": brute,
            "folder": "Cardiologie",
            "quartile_min": "2",
            "citedness_min": "",
            "sort": "date_desc",
        },
    )
    assert response.status_code == 200

    with get_conn(client.db_path) as conn:
        created = [q for q in repo.list_queries(conn) if q.name == "Nouvelle veille"][0]
    assert created.pubmed_query == brute.strip()  # seuls les espaces de bord sautent
    assert created.quartile_min == 2
    assert created.citedness_min is None  # champ vide = pas de filtre, pas zéro


def test_creation_rafraichit_la_barre_laterale_hors_bande(client: TestClient) -> None:
    response = client.post(
        "/queries/new",
        data={"name": "Veille B", "pubmed_query": "x[tiab]", "folder": "Autre",
              "quartile_min": "", "citedness_min": "", "sort": "date_desc"},
    )
    # Un seul aller-retour met à jour le panneau et la colonne de gauche.
    assert 'hx-swap-oob="innerHTML:#barre-laterale"' in response.text
    assert "Veille B" in response.text


def test_edition_met_a_jour_sans_toucher_aux_articles(client: TestClient) -> None:
    query_id = client.query_id
    response = client.post(
        f"/queries/{query_id}/edit",
        data={"name": "Renommée", "pubmed_query": '"N Engl J Med"[ta]',
              "folder": "Références", "quartile_min": "", "citedness_min": "3,5",
              "sort": "journal"},
    )
    assert response.status_code == 200
    assert "Semaglutide in Heart Failure" in response.text  # article conservé

    with get_conn(client.db_path) as conn:
        query = repo.get_query(conn, query_id)
    assert query.name == "Renommée"
    assert query.quartile_min is None
    assert query.citedness_min == 3.5  # virgule décimale acceptée
    assert query.sort == "journal"


def test_suppression_retire_la_requete_et_garde_les_articles(client: TestClient) -> None:
    response = client.delete(f"/queries/{client.query_id}")
    assert response.status_code == 200
    assert "Requête supprimée" in response.text

    with get_conn(client.db_path) as conn:
        assert repo.get_query(conn, client.query_id) is None
        # L'article reste en cache pour les autres requêtes.
        assert repo.get_article_summary(conn, "40000001") is not None


def test_article_non_lu_est_mis_en_evidence(client: TestClient) -> None:
    response = client.get(f"/queries/{client.query_id}/articles")
    assert 'class="article non-lu"' in response.text
    assert "marquer lu" in response.text


def test_marquage_lu_bascule_et_met_a_jour_le_compteur(client: TestClient) -> None:
    response = client.post(
        "/articles/40000001/read",
        data={"query_id": client.query_id, "read": 1},
    )
    assert response.status_code == 200
    # La ligne rejouée n'est plus en évidence et propose la bascule inverse.
    assert 'class="article"' in response.text
    assert '"read": 0' in response.text
    # Le compteur de non-lus suit, hors bande.
    assert 'hx-swap-oob="innerHTML:#barre-laterale"' in response.text
    assert "à jour" in response.text

    with get_conn(client.db_path) as conn:
        assert repo.count_unread(conn, client.query_id) == 0

    # Retour à non lu.
    retour = client.post(
        "/articles/40000001/read", data={"query_id": client.query_id, "read": 0}
    )
    assert 'class="article non-lu"' in retour.text


def test_article_nouveau_depuis_la_derniere_ouverture(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # L'article a été vu pour la première fois après l'ouverture précédente.
    monkeypatch.setattr(app.state, "new_since", "2020-01-01T00:00:00+00:00")
    assert "nouveau" in client.get(f"/queries/{client.query_id}/articles").text

    # Rien de nouveau si l'application a déjà été ouverte depuis.
    monkeypatch.setattr(app.state, "new_since", "2099-01-01T00:00:00+00:00")
    assert 'class="badge nouveau"' not in client.get(
        f"/queries/{client.query_id}/articles"
    ).text


def test_premiere_ouverture_ne_signale_aucun_nouveau(client: TestClient) -> None:
    # Au tout premier lancement il n'y a pas d'ouverture précédente : rien n'est
    # signalé comme nouveau, et l'ouverture est enregistrée pour la prochaine fois.
    assert 'class="badge nouveau"' not in client.get(
        f"/queries/{client.query_id}/articles"
    ).text
    with get_conn(client.db_path) as conn:
        assert repo.get_app_state(conn, repo.LAST_OPENED_KEY) is not None


def test_resume_efetch_a_la_demande_puis_cache(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePubMedClient()
    monkeypatch.setattr("app.main.PubMedClient", lambda: fake)

    panel = f"/articles/40000001/panel?query_id={client.query_id}"
    response = client.get(panel)
    assert response.status_code == 200
    assert "OBJECTIF : Évaluer le sémaglutide." in response.text
    assert "RÉSULTATS : Amélioration nette." in response.text
    assert fake.efetch_calls == 1

    # Deuxième ouverture : servie depuis la base, sans rappeler PubMed.
    again = client.get(panel)
    assert "OBJECTIF : Évaluer le sémaglutide." in again.text
    assert fake.efetch_calls == 1

    with get_conn(client.db_path) as conn:
        assert repo.get_abstract(conn, "40000001") is not None


def test_resume_absent_est_memorise_pour_ne_pas_rappeler_pubmed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePubMedClient(abstract=None)
    monkeypatch.setattr("app.main.PubMedClient", lambda: fake)

    panel = f"/articles/40000001/panel?query_id={client.query_id}"
    assert "Aucun résumé disponible" in client.get(panel).text
    client.get(panel)
    assert fake.efetch_calls == 1  # l'absence de résumé est elle aussi en cache


def test_resume_d_un_article_inconnu_renvoie_404(client: TestClient) -> None:
    url = f"/articles/99999999/panel?query_id={client.query_id}"
    assert client.get(url).status_code == 404


def test_le_rafraichissement_ne_recupere_aucun_resume(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Garde-fou : afficher la liste ne doit jamais appeler PubMed, seulement
    # proposer de quoi demander le résumé. Le pipeline reste léger et poli.
    fake = FakePubMedClient()
    monkeypatch.setattr("app.main.PubMedClient", lambda: fake)

    response = client.get(f"/queries/{client.query_id}/articles")
    assert fake.efetch_calls == 0
    assert "OBJECTIF : Évaluer le sémaglutide." not in response.text
    assert (
        f'hx-get="/articles/40000001/panel?query_id={client.query_id}"' in response.text
    )


def test_apercu_sans_chaine_n_appelle_pas_pubmed(client: TestClient) -> None:
    response = client.get("/preview")
    assert response.status_code == 200
    assert "Saisis une chaîne" in response.text


def test_apercu_rapporte_le_decompte_et_la_traduction(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.main.PubMedClient", lambda: FakePubMedClient())
    response = client.get("/preview", params={"pubmed_query": '"N Engl J Med"[ta]'})
    assert response.status_code == 200
    assert "1 234" in response.text  # séparateur de milliers français
    assert "[Journal]" in response.text  # QueryTranslation rendue par PubMed
    assert "⚠" in response.text  # avertissement PubMed remonté tel quel
    # Lien aller-retour vers l'Advanced Search Builder.
    assert "https://pubmed.ncbi.nlm.nih.gov/?term=" in response.text


def test_resolveur_ignore_les_termes_trop_courts(client: TestClient) -> None:
    response = client.get("/journals/resolve", params={"q": "ne"})
    assert response.status_code == 200
    assert "<li>" not in response.text


def test_l_etoile_complete_les_metadonnees_de_citation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Les articles d'avant la migration (authors NULL) sont completes au clic."""
    with get_conn(client.db_path) as conn:
        conn.execute("UPDATE articles SET authors = NULL WHERE pmid = '40000001'")
    complet = ArticleSummary(
        pmid="40000001",
        title="Semaglutide in Heart Failure",
        journal="N Engl J Med",
        issn="00284793",
        eissn=None,
        pub_date="2026-08-13",
        authors="Kosiborod MN; Abildstrom SZ",
        journal_full="The New England journal of medicine",
        volume="390",
        issue="2",
        pages="128-139",
    )
    fake = FakePubMedClient(summaries=[complet])
    monkeypatch.setattr("app.main.PubMedClient", lambda: fake)

    reponse = client.post(
        "/articles/40000001/favorite",
        data={"favorite": 1, "query_id": client.query_id},
    )
    assert reponse.status_code == 200
    assert fake.esummary_calls == [["40000001"]]
    with get_conn(client.db_path) as conn:
        assert repo.get_article_summary(conn, "40000001").volume == "390"


def test_l_etoile_tient_meme_si_pubmed_est_injoignable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le favori vient d'etre pose : une panne reseau ne doit pas l'annuler."""
    with get_conn(client.db_path) as conn:
        conn.execute("UPDATE articles SET authors = NULL WHERE pmid = '40000001'")
    monkeypatch.setattr(
        "app.main.PubMedClient", lambda: FakePubMedClient(en_panne=True)
    )

    reponse = client.post(
        "/articles/40000001/favorite",
        data={"favorite": 1, "query_id": client.query_id},
    )
    assert reponse.status_code == 200
    with get_conn(client.db_path) as conn:
        assert repo.count_favorites(conn) == 1


class FakePubMedClient:
    """Simule PubMedClient pour les routes qui appellent PubMed."""

    def __init__(
        self, abstract: str | None = "OBJECTIF : Évaluer le sémaglutide.\n\n"
        "RÉSULTATS : Amélioration nette.",
        summaries: list | None = None,
        en_panne: bool = False,
    ) -> None:
        self._abstract = abstract
        self._summaries = summaries or []
        self._en_panne = en_panne
        self.efetch_calls = 0
        self.esummary_calls: list[list[str]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def efetch_abstract(self, pmid: str) -> str | None:
        self.efetch_calls += 1
        return self._abstract

    async def esummary(self, pmids: list[str]):
        self.esummary_calls.append(pmids)
        if self._en_panne:
            raise httpx.ConnectError("reseau injoignable")
        return [resume for resume in self._summaries if resume.pmid in pmids]

    async def esearch(self, term, *, reldate_days=None, retmax=None):
        from app.models import ESearchResult

        return ESearchResult(
            pmids=[],
            count=1234,
            translation='"N Engl J Med"[Journal]',
            warnings=["terme ignoré : blabla"],
            errors=[],
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-08-13", "13/08/2026"),
        ("2026-08", "08/2026"),  # date PubMed partielle
        ("2026", "2026"),
        ("2026-08-15T21:30:00+00:00", "15/08/2026 à 21:30"),
        (None, "date inconnue"),
    ],
)
def test_date_fr(value: str | None, expected: str) -> None:
    assert date_fr(value) == expected
