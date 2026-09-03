# Nouveautés transversales et recherche locale — design

> Validé le 2026-09-03. Complète `spec.md`, qui reste la source de vérité du
> projet. Ce document ne décrit que les trois chantiers ci-dessous.

## Pourquoi

Trois manques constatés sur l'application en service :

1. Le badge « nouveau » s'ancre sur `app_state.last_opened_at`, écrit au
   **démarrage du serveur**. Sous Docker avec `restart: unless-stopped`, le
   process ne redémarre jamais : le repère se fige au premier lancement et le
   badge cesse de signaler quoi que ce soit.
2. La consultation se fait requête par requête. Rien ne répond à « qu'est-ce
   qui est arrivé depuis ma dernière session, toutes requêtes confondues ? ».
3. Aucun moyen de retrouver un article déjà vu dans le cache local, et
   `list_articles` n'a aucune limite : une requête large rend des milliers de
   lignes d'un coup.

## Décisions

### « Nouveau » s'ancre sur la visite de la requête

Deux colonnes sur `queries` :

| colonne | rôle |
|---|---|
| `last_visited_at` | date de la dernière ouverture de la requête |
| `previous_visit_at` | **le repère du badge** |

À chaque ouverture d'une requête :

```
si last_visited_at IS NULL          → previous ← created_at ; last ← now
sinon si now − last_visited_at > seuil → previous ← last_visited_at ; last ← now
sinon                                 → last ← now   (le repère ne bouge pas)
```

Le badge vaut `first_seen > previous_visit_at`.

Le **seuil de session** (`VISIT_SESSION_GAP_MINUTES`, défaut 30) est ce qui
rend le modèle vivable : recharger la liste dans la foulée n'efface plus les
badges, seule une vraie nouvelle session décale le repère.

Conséquences :

- `record_app_opening()`, `LAST_OPENED_KEY` et `_new_since()` disparaissent.
  La table `app_state` reste — infrastructure générique, elle resservira.
- Une requête créée puis rafraîchie affiche **tous** ses articles comme
  nouveaux (`previous_visit_at = created_at`), ce qui n'était pas le cas avant.
- Les requêtes déjà en base sont calées sur la date de migration, pour ne pas
  déverser tout l'historique dans le flux au premier lancement.

Point assumé : une requête GET écrit désormais en base. C'est le principe d'un
lecteur de flux, et l'usage est mono-utilisateur sur réseau local.

### Flux « Nouveautés » transversal

`GET /new` — entrée en tête de barre latérale, au-dessus de la Bibliographie,
avec compteur. Liste les paires `(requête, article)` où
`first_seen > previous_visit_at`, toutes requêtes confondues, triées par
`first_seen` décroissant, chaque ligne portant le nom de sa requête d'origine.

**Le flux ne consomme rien** : il ne décale aucun `previous_visit_at`, sinon
l'ouvrir viderait tout d'un coup. Le repère ne bouge qu'à l'ouverture d'une
requête précise, ou via le bouton explicite « Tout marquer comme vu ».

### Recherche locale et pagination

Table virtuelle `articles_fts` en `content='articles'` (external content, pas
de duplication du texte) sur `title`, `abstract`, `journal_full`, `authors`,
maintenue par trois triggers. `GET /search?q=`, champ dans la barre latérale,
résultats classés par `rank`.

Réserve connue : `abstract` reste NULL tant que l'article n'a pas été déplié
(§6 de `spec.md`). L'index porte donc surtout sur titres, revues et auteurs, et
s'enrichit au fil des lectures via le trigger UPDATE. Aucun préchargement des
résumés — ce serait contraire au spec et impoli envers le NCBI. L'interface le
dit en une ligne.

La saisie est échappée avant d'atteindre `MATCH` : chaque terme entre
guillemets, joints par AND, joker sur le dernier terme. Une apostrophe ou un
tiret ne doit jamais remonter une erreur SQLite.

Pagination : `LIMIT/OFFSET`, 50 par page, bouton « Charger la suite » en
`hx-swap="beforeend"`, appliqué aux trois listes — articles, flux, recherche.

## Phases

| Phase | Contenu | Vérification |
|---|---|---|
| **A** | Hygiène : secret, résidus macOS, venv recréé | `uv run pytest` vert |
| **B** | Migration seule : 2 colonnes + FTS5 + triggers + peuplement | `PRAGMA table_info` ; `count(articles_fts) == count(articles)` |
| **C** | Badge « nouveau » sur la visite | tests ajustés, parcours manuel |
| **D** | Flux « Nouveautés » | compteur cohérent avec la somme des requêtes |
| **E** | Recherche locale | requêtes de référence A et B du spec en non-régression |
| **F** | Pagination des trois listes | chargement incrémental sur une liste longue |

Un commit par phase. Le schéma est migré en phase B, séparément de la logique
applicative, comme l'exige `CLAUDE.md`. Aucune phase ne touche au pipeline de
filtrage qualité ni à la chaîne PubMed.

## Comment annuler

Quatre niveaux, du plus fin au plus radical.

1. **Une phase** — `git revert <sha>` du commit de la phase.
2. **Tout le chantier** — `git checkout master`. Le point de retour est
   `1b2b8d5`. La branche de travail est `feature/nouveautes-et-recherche`.
3. **Les données** — `init_db()` prend une sauvegarde horodatée avant toute
   migration d'une base peuplée, dans `data/backups/` : à côté de la base, donc
   dans le volume monté en conteneur, et ignoré par git. Elle passe par l'API
   `backup()` de sqlite3 et non par une copie de fichier, le mode WAL rendant
   le `.db` seul incomplet. Restaurer = remettre le fichier en place, serveur
   arrêté.
4. **Rien à faire, en fait** — les changements de schéma sont **additifs** :
   colonnes ajoutées et table FTS séparée. Le code de `master` fonctionne tel
   quel sur une base migrée, il ignore simplement ce qu'il ne connaît pas.
   Revenir en arrière côté code suffit ; la base n'a pas besoin d'être touchée.

Le seul résidu d'un retour en arrière serait la ligne `last_opened_at` de
`app_state`, devenue inutile puis réutilisée telle quelle par l'ancien code.
