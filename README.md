# AutoPubMed

Lecteur de veille bibliographique médicale, **local et auto-hébergé**.

On enregistre des requêtes PubMed, organisées en dossiers. En cliquant sur une
requête, on voit les derniers articles qui y répondent — **filtrés par qualité
de revue** (quartile SJR, citedness OpenAlex), ce que PubMed ne sait pas faire —
avec suivi des articles non lus, flux des nouveautés et recherche plein texte
dans le cache local. Une notation de pertinence par requête apprend de tes
notes pour trier et masquer les articles hors sujet.

C'est un *mini-lecteur PubMed personnel*, pas un agrégateur. Seules API
utilisées : E-utilities (NCBI), OpenAlex et Scimago.

Le cahier des charges complet est dans [`spec.md`](spec.md) ; les règles de
contribution dans [`CLAUDE.md`](CLAUDE.md).

## Stack

- Python 3.12+, environnement géré par [`uv`](https://docs.astral.sh/uv/)
- FastAPI + Jinja2 + HTMX (fragments HTML, pas de SPA)
- SQLite (`sqlite3` standard, mode WAL)
- `httpx` pour les appels sortants, APScheduler pour le job de fond
- `ruff` (lint + format), `pytest`

## Démarrage

### 1. Prérequis

Installer `uv` : <https://docs.astral.sh/uv/getting-started/installation/>

### 2. Configuration

```bash
cp .env.example .env
```

Renseigner au minimum `NCBI_EMAIL` et `OPENALEX_MAILTO` (adresses réelles :
elles identifient l'appelant auprès du NCBI et d'OpenAlex). Une `NCBI_API_KEY`
est optionnelle mais fait passer le débit de 3 à 10 req/s. Voir les commentaires
de `.env.example` pour le reste.

### 3. Données Scimago (une fois)

Télécharger le classement Scimago (CSV `;`) depuis
<https://www.scimagojr.com/journalrank.php?out=xls>, puis :

```bash
uv run python -m app.scimago_import <chemin_du_csv>
```

À rafraîchir manuellement une fois par an.

### 4. Lancer

```bash
uv run autopubmed              # serveur + ouverture du navigateur
uv run uvicorn app.main:app --reload   # mode développement (rechargement à chaud)
```

Sous Windows, `lancer.cmd` fait tout (double-clic) : `uv sync` puis
`uv run autopubmed`.

## Notation de pertinence

Dans l'encart de lecture, chaque article se note **dans la requête ouverte** :
1 · hors sujet, 2 · secondaire, 3 · pertinent (re-cliquer retire la note).

- **Apprentissage** : à partir de 8 notes dont au moins un 3 et un 1 ou 2,
  les articles non notés reçoivent une pertinence prédite (« ≈ 2,6 »), par
  plus proches voisins sur le titre et la revue. L'encart montre l'article
  noté le plus proche, qui explique la prédiction.
- **Tri** : choisir « Pertinence » dans le formulaire de la requête.
- **Masquage** : champ « Masquer les peu pertinents » du même formulaire
  (< 1,5, < 2 ou < 2,5). Rien n'est supprimé : « N masqués · afficher » en
  tête de liste les fait réapparaître, et revenir à « ne rien masquer » rend
  la liste d'avant.
- **Affiner la chaîne** : le bloc « Affiner la chaîne d'après tes notes »
  propose des fragments `AND "x"[tiab]` / `NOT "y"[tiab]` tirés des titres
  notés 3 et 1. Rien n'est inséré : on colle ce qu'on retient, l'aperçu
  PubMed en mesure l'effet.

Avant d'activer le masquage, vérifier que le modèle tient la route sur la
requête (erreur leave-one-out et matrice de confusion) :

```bash
uv run python -m app.relevance --id <N>
```

`--all` recalcule les prédictions de toutes les requêtes.

### Retour arrière

Du plus léger au plus complet :

1. **Désactiver** : `ENABLE_RELEVANCE=0` dans `.env`, puis relancer.
   L'interface redevient celle d'avant la fonction ; les notes restent en
   base, intactes.
2. **Désinstaller** : sauvegarde de la base, export des notes en JSON dans
   `data/backups/`, puis suppression des trois tables de la fonction (aucune
   autre table n'est touchée) :

   ```bash
   uv run python -m app.relevance_admin --desinstaller
   ```

   Pour restaurer les notes plus tard :

   ```bash
   uv run python -m app.relevance_admin --importer data/backups/notes-AAAAMMJJ-HHMMSS.json
   ```

   `--db <chemin>` vise une autre base, par exemple une copie.
3. **Revenir au code d'avant** : le tag git `avant-notation` marque le dernier
   commit sans la fonction. Une sauvegarde de la base est aussi prise
   automatiquement dans `data/backups/` avant la migration qui crée les
   tables.

## Docker

```bash
docker compose up -d
```

L'interface est alors sur <http://localhost:8005>. Le volume `./data` porte la
base SQLite et ses sauvegardes ; `./.env` est monté au démarrage (jamais dans
l'image). Le job de fond est activé (`ENABLE_SCHEDULER=1`) dans `compose`.

## Développement

```bash
uv sync                        # dépendances (dont le groupe dev)
uv run ruff check .            # lint
uv run ruff format .           # format
uv run pytest                  # tests
```

## Structure

```
app/
  main.py          points d'entrée FastAPI (fragments HTML pour HTMX)
  refresh.py       pipeline refresh_query : chercher → enrichir → qualifier → filtrer → persister
  repo.py          tout l'accès SQL (fonctions de dépôt fines autour de sqlite3)
  pubmed.py        client E-utilities (esearch / esummary / efetch / nlmcatalog)
  openalex.py      client OpenAlex (citedness)
  quality.py       application des filtres quartile / citedness
  scimago_import.py  chargement du CSV Scimago dans journal_quality
  scheduler.py     job de fond APScheduler
  relevance.py     notation de pertinence : prédiction, explication, termes suggérés
  relevance_admin.py  désinstallation / réimport des notes
  db.py            connexions SQLite, migrations, sauvegardes
  schema.sql       schéma (idempotent)
  schema_relevance.sql  tables de la couche pertinence (ajout seul)
  models.py        dataclasses partagées
  templates/       gabarits Jinja2
  static/          CSS et HTMX
ollama/Modelfile   variante de prompt embarquée dans un modèle Ollama
tests/             pytest
```

## Principes non négociables

Détaillés dans `CLAUDE.md`. En résumé : la chaîne PubMed brute est la source de
vérité (jamais réécrite ni validée localement) ; les filtres qualité sont des
champs séparés appliqués **après** récupération, jamais fusionnés dans la
requête PubMed ; l'interface lit la base, elle n'appelle pas PubMed en direct
(sauf aperçu live et bouton « Rafraîchir »).

## Licence

MIT — voir [`LICENSE`](LICENSE).
