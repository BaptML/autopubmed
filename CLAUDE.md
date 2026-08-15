# CLAUDE.md — Règles du projet PubMed Veille

## Contexte

Lecteur de veille bibliographique médicale, local et auto-hébergé. Requêtes
PubMed sauvegardées en dossiers, filtre qualité de revue (quartile SJR +
citedness OpenAlex), suivi des non-lus. **Le cahier des charges complet est dans
`spec.md` — c'est la source de vérité. Lis-le avant toute planification.**

Réponds-moi en français.

## Stack & outils

- Python 3.12+, environnement géré par **`uv`**
- FastAPI + Jinja2 + **HTMX** (fragments HTML, pas de SPA JavaScript)
- SQLite via le module standard `sqlite3`, en **mode WAL**, une connexion par
  requête/tâche
- `httpx` (async) pour les appels API
- APScheduler pour le job de fond
- `uvicorn` pour servir

Garde les dépendances minimales. Pas d'ORM lourd : des fonctions de dépôt fines
autour de `sqlite3`.

## Commandes

- Installer : `uv sync`
- Lancer en dev : `uv run uvicorn app.main:app --reload`
- Lancer un script ponctuel : `uv run python -m app.<module>`
- Importer Scimago : `uv run python -m app.scimago_import <chemin_csv>`

## Conventions de code

- Identifiants, noms de fonctions et de tables : en anglais.
- Textes de l'interface (labels, messages) : en français.
- Commentaires et docstrings : en français, concis, seulement là où le *pourquoi*
  n'est pas évident.
- Dates stockées en ISO 8601 (chaînes).
- Type hints partout ; fonctions courtes et testables.

## Règles absolues (interdits)

Ces règles priment sur toute « amélioration » spontanée. Ne les enfreins pas.

1. **Ne construis pas de constructeur de requête visuel à clics.** Le mode clics
   est délégué à l'Advanced Search Builder de PubMed via un lien aller-retour.
2. **Ne parse pas et ne valide pas la syntaxe PubMed localement.** La validation
   est déléguée à ESearch (nombre de résultats + `QueryTranslation`).
3. **Ne fusionne jamais les filtres qualité dans la chaîne PubMed.** Quartile et
   citedness sont des champs séparés, appliqués après récupération.
4. **N'introduis aucune application ou agrégateur tiers.** Uniquement les API
   PubMed / OpenAlex / Scimago et notre code.
5. **La chaîne PubMed brute est la source de vérité** d'une requête. Ne la
   réécris pas, ne la « normalise » pas silencieusement.
6. **Modèle cache** : l'UI lit la base, elle n'appelle pas PubMed en direct
   (sauf l'aperçu live et le bouton « rafraîchir » explicite).

## Étiquette API & secrets

- Clé API NCBI, adresse e-mail de contact et `mailto` OpenAlex vivent dans un
  fichier `.env` **jamais commité** (ajoute-le au `.gitignore`).
- Passe toujours `tool` + `email` aux E-utilities, et `mailto` à OpenAlex
  (polite pool).
- Respecte les limites de débit : ≤ 10 req/s NCBI avec clé ; regroupe les PMID
  par lots dans `esummary`/`efetch` plutôt que d'appeler un par un.
- Récupère les résumés (`efetch`) uniquement à la demande, pas pendant le
  rafraîchissement.

## Méthode de travail

Suis le cycle **Explorer → Planifier → Coder → Vérifier**.

- Commence en **plan mode** : lis `spec.md` et ce fichier, puis propose un plan
  découpé en phases livrables (voir §10 du spec) avec, pour chacune, comment la
  vérifier. **Ne modifie aucun fichier avant que j'aie approuvé le plan.**
- Travaille **une phase à la fois**, avec un commit git à la fin de chaque phase.
- Fais le **plus petit changement cohérent** ; ne prends pas d'avance sur les
  phases suivantes.
- Pour explorer une API que tu connais mal, délègue l'exploration à un
  sous-agent pour ne rapporter que les conclusions.
- Utilise `/clear` entre les phases pour garder le contexte propre.

## Attentes de planification

Pour chaque plan que tu proposes :
- nomme les fichiers touchés et les risques ;
- sépare la migration du schéma SQLite de la logique applicative ;
- prévois la vérification contre les **deux requêtes de référence** du spec
  (A : filtre Q1 respecté ; B : NEJM + RCT uniquement) dès qu'elles sont
  exécutables ;
- annonce ce qui reste à faire après la phase.
