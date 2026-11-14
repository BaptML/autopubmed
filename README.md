# AutoPubMed

Un lecteur de veille bibliographique médicale qui tourne chez toi. Tu
enregistres tes requêtes PubMed, et AutoPubMed te montre les nouveaux articles
qui y répondent, en ne gardant que ceux des revues que tu juges assez bonnes.

## Pourquoi

PubMed sait chercher, mais ne sait pas trier par qualité de revue. Une alerte
sur un sujet large ramène autant d'articles de revues confidentielles que
d'essais publiés dans les grands journaux, et c'est à toi de faire le tri à
chaque fois.

AutoPubMed fait ce tri. Tu écris ta requête comme dans PubMed, tu ajoutes un
quartile minimal (classement Scimago) ou un seuil de citedness (OpenAlex,
l'équivalent gratuit du facteur d'impact), et la liste ne contient plus que ce
qui passe.

## Ce que ça fait

- Requêtes PubMed sauvegardées et rangées en dossiers, avec un aperçu du
  nombre de résultats avant d'enregistrer.
- Filtre par qualité de revue, réglé séparément pour chaque requête.
- Suivi des non-lus, flux des nouveautés toutes requêtes confondues, et
  recherche plein texte dans les articles déjà récupérés.
- Rafraîchissement automatique en arrière-plan, toutes les six heures par
  défaut.
- Notation de pertinence : tu notes quelques articles de 1 à 3, et
  l'application trie ou masque les autres d'après tes notes.
- Résumé en français de l'abstract par un modèle local (Ollama), si tu en as
  un. Rien ne sort de ta machine.

Tout est stocké dans un fichier SQLite local. Les seuls services contactés sont
les E-utilities du NCBI, OpenAlex et, une fois par an et à la main, Scimago.

## Installation

Il te faut [`uv`](https://docs.astral.sh/uv/getting-started/installation/),
qui installe Python et les dépendances tout seul.

Copie d'abord le fichier de configuration :

```bash
cp .env.example .env
```

Renseigne au minimum `NCBI_EMAIL` et `OPENALEX_MAILTO` avec une vraie adresse :
le NCBI et OpenAlex demandent à savoir qui les appelle. Une `NCBI_API_KEY` est
facultative, elle fait passer le débit autorisé de 3 à 10 requêtes par seconde.
Les autres réglages sont commentés dans `.env.example`.

Charge ensuite le classement des revues. Télécharge le CSV sur
[scimagojr.com](https://www.scimagojr.com/journalrank.php?out=xls), puis :

```bash
uv run python -m app.scimago_import <chemin_du_csv>
```

Scimago publie un nouveau classement chaque année, donc l'opération est à
refaire une fois par an.

Il ne reste qu'à lancer :

```bash
uv run autopubmed
```

Le serveur démarre et ton navigateur s'ouvre sur l'application. Sous Windows,
un double-clic sur `lancer.cmd` fait la même chose.

## Sur un serveur, avec Docker

```bash
docker compose up -d
```

L'interface est sur <http://localhost:8005>. La base et ses sauvegardes vivent
dans `./data`, hors du conteneur, et `.env` est lu au démarrage sans jamais
entrer dans l'image. Le rafraîchissement automatique est activé d'office.

Pour mettre à jour : `git pull`, puis `docker compose up -d --build`.

L'application n'a pas de mot de passe. Garde-la sur ton réseau local.

## Notation de pertinence

Quand un article est ouvert, tu peux le noter pour la requête en cours : 1 pour
hors sujet, 2 pour secondaire, 3 pour pertinent. Un second clic sur la même
note la retire.

À partir de 8 notes, dont au moins un 3 et au moins un 1 ou 2, les articles non
notés reçoivent une note prédite, affichée sous la forme « ≈ 2,6 ». Le calcul
compare le titre et la revue de l'article à ceux que tu as notés, et le panneau
de lecture montre l'article noté le plus proche pour que tu saches d'où vient
la prédiction.

Trois usages en découlent, tous dans le formulaire de la requête :

- trier la liste par pertinence ;
- masquer les articles sous un seuil (1,5, 2 ou 2,5). Rien n'est supprimé : le
  lien « N masqués · afficher » en tête de liste les fait revenir ;
- affiner ta chaîne PubMed. Le bloc « Affiner la chaîne d'après tes notes »
  propose des fragments `AND "x"[tiab]` ou `NOT "y"[tiab]` tirés des titres
  que tu as notés 3 et 1. Tu colles ceux qui te conviennent, l'application ne
  modifie jamais ta chaîne d'elle-même.

Avant d'activer le masquage sur une requête, regarde si le modèle y est
fiable. Cette commande affiche son erreur moyenne, comparée à celle d'une
prédiction naïve :

```bash
uv run python -m app.relevance --id <N>
```

Avec `--all` à la place de `--id`, elle recalcule les prédictions de toutes les
requêtes.

### Si tu n'en veux pas

Mets `ENABLE_RELEVANCE=0` dans `.env` et relance. La fonction disparaît de
l'interface et tes notes restent en base.

Pour la retirer complètement, cette commande sauvegarde la base, exporte tes
notes en JSON dans `data/backups/`, puis supprime les trois tables de la
fonction sans toucher aux autres :

```bash
uv run python -m app.relevance_admin --desinstaller
```

Tu pourras réimporter tes notes plus tard :

```bash
uv run python -m app.relevance_admin --importer data/backups/notes-AAAAMMJJ-HHMMSS.json
```

Ajoute `--db <chemin>` pour viser une autre base, une copie par exemple. Le tag
git `avant-notation` marque le dernier commit sans la fonction.

## Choix de conception

Quelques règles expliquent pourquoi l'application se comporte comme elle le
fait.

Ta chaîne PubMed est enregistrée telle que tu l'as écrite. AutoPubMed ne la
corrige pas et ne la valide pas lui-même : c'est PubMed qui dit combien de
résultats elle ramène et comment il l'a comprise. Pour construire une requête à
la souris, un lien t'envoie vers l'Advanced Search Builder de PubMed.

Les filtres de qualité sont des champs à part, appliqués après la réponse de
PubMed. Ils n'entrent jamais dans la chaîne.

L'interface lit la base locale. Elle n'interroge PubMed que pour l'aperçu d'une
requête et quand tu cliques sur « Rafraîchir », ce qui la garde rapide et
utilisable hors ligne.

Le cahier des charges complet est dans [`spec.md`](spec.md).

## Développement

L'application est écrite en Python 3.12 avec FastAPI, Jinja2 et HTMX : le
serveur renvoie des fragments HTML, sans framework JavaScript. Les données sont
dans SQLite, via le module `sqlite3` standard.

```bash
uv sync                                  # installe les dépendances
uv run uvicorn app.main:app --reload     # serveur avec rechargement à chaud
uv run ruff check . && uv run ruff format --check . && uv run pytest
```

La dernière ligne est la vérification à passer avant chaque commit.

```
app/
  main.py               routes FastAPI (fragments HTML pour HTMX)
  refresh.py            rafraîchissement d'une requête, de PubMed à la base
  repo.py               tout l'accès SQL
  pubmed.py             client E-utilities
  openalex.py           client OpenAlex (citedness)
  quality.py            filtres quartile et citedness
  scimago_import.py     import du CSV Scimago
  scheduler.py          rafraîchissement en arrière-plan
  relevance.py          notation de pertinence
  relevance_admin.py    retrait et réimport des notes
  llm.py                résumé français par Ollama
  db.py                 connexions SQLite, migrations, sauvegardes
  schema.sql            schéma de la base
  schema_relevance.sql  tables de la notation de pertinence
  templates/            gabarits Jinja2
  static/               CSS et HTMX
ollama/Modelfile        prompt de résumé embarqué dans un modèle Ollama
tests/                  tests pytest
```

Les règles de contribution sont dans [`CLAUDE.md`](CLAUDE.md), et les idées en
attente dans [`TODO.md`](TODO.md).

## Licence

MIT, voir [`LICENSE`](LICENSE).
