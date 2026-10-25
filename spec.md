# AutoPubMed — Cahier des charges (spec.md)

> Ce fichier est la **source de vérité** du projet. Il décrit *quoi* construire.
> Les règles de travail et les interdits sont dans `CLAUDE.md`.

## 1. Objectif

Un lecteur de veille bibliographique **local et auto-hébergé** pour la
littérature médicale. L'utilisateur enregistre des requêtes PubMed, organisées
en dossiers. En cliquant sur une requête, il voit les derniers articles qui y
répondent — **filtrés par qualité de revue** (quartile SJR, citedness) que
PubMed ne sait pas filtrer — avec suivi des articles non lus.

C'est un *mini-lecteur PubMed personnel*, pas un agrégateur générique. Aucune
application tierce : uniquement les API publiques (PubMed / OpenAlex / Scimago)
et notre propre code.

## 2. Stack technique

- **Backend** : FastAPI (Python 3.12+)
- **Templates** : Jinja2
- **Interactivité** : HTMX (chargement de fragments HTML, pas de SPA JS)
- **Base** : SQLite via le module standard `sqlite3`, en mode **WAL**
  (une connexion par requête/tâche ; le job de fond et le serveur y accèdent
  en concurrence)
- **HTTP sortant** : `httpx` (async)
- **Planification** : APScheduler pour le job de fond
- **Environnement** : `uv`
- **Serveur** : `uvicorn`

Dépendances minimales, pas d'ORM lourd. Des fonctions de dépôt (repository)
fines encapsulent les accès SQLite.

## 3. Principes directeurs (non négociables)

1. **Deux couches de filtrage, jamais mélangées.**
   - *Couche native PubMed* (revue, type d'article, MeSH, dates, auteurs) : elle
     vit **dans la chaîne de requête PubMed**, telle quelle.
   - *Couche qualité* (quartile, citedness) : ce sont des **champs séparés** sur
     la requête, appliqués **après** récupération. Ne jamais injecter la qualité
     dans la chaîne PubMed.

2. **S'appuyer sur PubMed, ne pas le reconstruire.**
   - Pas de constructeur de requête visuel. Le « mode clics » est délégué à
     l'Advanced Search Builder de PubMed via un lien aller-retour.
   - On ne parse jamais et on ne valide jamais la syntaxe PubMed localement :
     c'est ESearch qui valide (nombre de résultats + `QueryTranslation`).
   - Les vocabulaires (noms de revue, MeSH) viennent des services NLM, pas d'une
     liste maison.

3. **Modèle cache, pas requête à chaud.**
   - Un job de fond rafraîchit les requêtes ; l'UI ne fait que lire la base →
     affichage instantané et badge « non lus » naturel via `first_seen`.
   - Un bouton « Rafraîchir » par requête force un appel à chaud à la demande.

4. **La chaîne PubMed brute est la source de vérité** stockée pour chaque
   requête. C'est elle, réexécutée, qui redonne le résultat.

5. **Couche pertinence : une troisième couche, séparée des deux autres.**
   - L'utilisateur note chaque article de 1 à 3 **dans une requête donnée**
     (1 = hors sujet, 2 = secondaire, 3 = pertinent) ; un même article peut
     être central dans une requête et hors sujet dans une autre.
   - Ces notes servent à **trier** et à **masquer de façon réversible**, à
     l'affichage seulement : le pipeline rattache toujours tous les articles
     qui passent la qualité, rien n'est supprimé.
   - Elle n'entre jamais dans la chaîne PubMed. Les termes qu'elle suggère
     sont affichés, à recopier par l'utilisateur ; l'aperçu ESearch en mesure
     l'effet.
   - Elle ne réadmet rien : un article écarté par la qualité ne revient pas
     parce qu'il serait jugé pertinent.
   - Drapeau `ENABLE_RELEVANCE` (défaut 1) : à 0, l'interface et les listes
     sont celles d'avant la fonction ; les notes restent en base.

## 4. Modèle de données (SQLite)

Tables principales (`schema.sql`) :

### `queries`
| champ | type | note |
|---|---|---|
| id | INTEGER PK | |
| name | TEXT | libellé affiché |
| folder | TEXT | nom du dossier (simple chaîne) |
| pubmed_query | TEXT | **la chaîne PubMed brute** |
| quartile_min | INTEGER NULL | 1–4 ; NULL = pas de filtre quartile |
| citedness_min | REAL NULL | seuil OpenAlex ; NULL = pas de filtre |
| sort | TEXT | tri d'affichage : `date_desc`, `date_asc`, `journal`, `relevance` |
| created_at | TEXT | ISO 8601 |
| last_refreshed_at | TEXT NULL | ISO 8601 |

### `articles`
| champ | type | note |
|---|---|---|
| pmid | TEXT PK | |
| title | TEXT | |
| abstract | TEXT NULL | rempli à la demande (voir §6) |
| journal | TEXT | abréviation NLM |
| issn | TEXT NULL | ISSN normalisé (voir §7) |
| pub_date | TEXT | ISO 8601 |
| url | TEXT | lien PubMed |
| fetched_at | TEXT | ISO 8601 |

### `query_articles` (table de jointure)
| champ | type | note |
|---|---|---|
| query_id | INTEGER | FK queries |
| pmid | TEXT | FK articles |
| first_seen | TEXT | ISO 8601 — date de première apparition |
| read | INTEGER | booléen 0/1 |

PK composite `(query_id, pmid)`.

### `journal_quality`
| champ | type | note |
|---|---|---|
| issn | TEXT PK | ISSN normalisé |
| journal_name | TEXT | |
| quartile | INTEGER NULL | 1–4, « SJR Best Quartile » |
| sjr | REAL NULL | score SJR |
| citedness | REAL NULL | OpenAlex `2yr_mean_citedness` |
| source | TEXT | `scimago` ou `openalex` |
| updated_at | TEXT | ISO 8601 |

### Couche pertinence (`schema_relevance.sql`)

Trois tables **ajoutées**, dans un fichier à part : aucune table ni colonne
existante n'est modifiée, une base migrée reste lisible par le code d'avant.
La migration est précédée d'une sauvegarde automatique dans `data/backups/`.
Clés étrangères `ON DELETE CASCADE` vers `queries` et `articles`.

#### `article_ratings`
| champ | type | note |
|---|---|---|
| query_id | INTEGER | FK queries |
| pmid | TEXT | FK articles |
| rating | INTEGER | 1–3 (`CHECK`) |
| rated_at | TEXT | ISO 8601 |

PK composite `(query_id, pmid)`.

#### `relevance_scores` (donnée dérivée, reconstructible)
| champ | type | note |
|---|---|---|
| query_id | INTEGER | FK queries |
| pmid | TEXT | FK articles |
| score | REAL | pertinence prédite, échelle 1–3 |
| computed_at | TEXT | ISO 8601 |

PK composite `(query_id, pmid)`.

#### `relevance_settings`
| champ | type | note |
|---|---|---|
| query_id | INTEGER PK | FK queries |
| hide_below | REAL NULL | seuil de masquage ; NULL = rien n'est masqué |

Table à part plutôt que colonne de `queries`, pour ne pas toucher à une table
existante.

## 5. Sources de données externes

### E-utilities (PubMed) — moteur de découverte
- Base : `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/`
- `esearch.fcgi` : `db=pubmed&term=<query>&reldate=<N>&datetype=edat&retmax=<M>&retmode=json`
  → renvoie `idlist`, `count`, `querytranslation`, et `warninglist`/`errorlist`.
- `esummary.fcgi` : `db=pubmed&id=<pmids>&retmode=json` → titre, `fulljournalname`,
  `source` (abréviation), `issn`/`essn`, `pubdate`. **Métadonnées légères, sans
  résumé.**
- `efetch.fcgi` : `db=pubmed&id=<pmid>&retmode=xml&rettype=abstract` → résumé
  (récupéré à la demande).
- `db=nlmcatalog` : résolution des noms de revue (NEJM → `N Engl J Med`).
- **Clé API NCBI** dans une variable d'environnement ; toujours passer `tool` et
  `email`. Avec clé : jusqu'à 10 req/s.

### OpenAlex — citedness (équivalent d'IF, gratuit)
- `https://api.openalex.org/sources?filter=issn:<ISSN>&mailto=<email>`
- Champ utile : `summary_stats.2yr_mean_citedness`.
- Interrogé **à la demande**, une fois par revue inconnue, puis mis en cache dans
  `journal_quality`.

### Scimago — quartiles SJR (chargement en masse, une fois)
- Téléchargement CSV : `https://www.scimagojr.com/journalrank.php?out=xls`
- CSV **séparé par des points-virgules**. Colonnes utiles : `Issn`, `SJR`,
  `SJR Best Quartile`, `Title`.
- Un script d'import remplit `journal_quality` (source = `scimago`).
  À rafraîchir manuellement une fois par an.

## 6. Le pipeline `refresh_query(query)`

Fonction cœur. L'UI ne fait que lire ; tout le travail est ici.

1. **Chercher** : `esearch` avec la `pubmed_query` + fenêtre de récence
   (`reldate`, `datetype=edat`). Récupérer la liste de PMID.
2. **Enrichir** : pour les PMID absents de `articles`, appeler `esummary`
   (par lots) → titre, revue, ISSN, date. Le **résumé n'est PAS récupéré ici**
   (voir plus bas).
3. **Qualifier** : pour chaque ISSN, chercher dans `journal_quality`. Si absent,
   remplir à la demande depuis OpenAlex (citedness) ; le quartile vient de la
   table Scimago préchargée.
4. **Filtrer (couche qualité)** : garder les articles dont
   `quartile <= quartile_min` **et** `citedness >= citedness_min` (les filtres
   NULL ne s'appliquent pas). Voir §7 pour les revues non classées.
5. **Persister** : upsert dans `articles` ; upsert dans `query_articles` avec
   `first_seen = now` et `read = 0` pour les paires nouvelles.
6. **Marquer** : mettre à jour `query.last_refreshed_at`.
7. **Prédire la pertinence** (si `ENABLE_RELEVANCE`) : recalculer
   `relevance_scores` pour la requête. Cette étape vient après le rattachement
   et n'a aucun effet sur lui.

**Moteur de prédiction** (`app/relevance.py`, sans dépendance) :
- représentation : titre + revue, minuscules, sans diacritiques, sans mots
  vides, unigrammes et bigrammes, pondérés TF-IDF sur les articles de la
  requête. Le résumé n'est pas utilisé : il n'est en cache que pour les
  articles ouverts, la comparaison serait faussée ;
- prédiction : k = 10 plus proches voisins notés (cosinus), moyenne pondérée
  tirée vers la note moyenne de la requête :
  `score = (Σ sim·note + α·moyenne) / (Σ sim + α)`, α = 0,5 ;
- un article noté a pour score sa note : le modèle ne contredit jamais
  l'utilisateur ;
- démarrage à froid : aucune prédiction tant que la requête n'a pas 8 notes
  dont au moins un 3 et au moins un 1 ou 2 ;
- recalcul aussi après chaque changement de note.

**Résumés à la demande** : `articles.abstract` reste NULL jusqu'à ce que
l'utilisateur déplie l'article. À ce moment, `efetch` récupère le résumé, qui
est ensuite mis en cache. Cela garde le rafraîchissement rapide et poli.

## 7. Détails du filtrage qualité

- **Quartile** : on utilise le « SJR Best Quartile » (meilleur quartile toutes
  catégories). Stocké en entier 1–4. Filtre `quartile_min = 2` → on garde Q1 et
  Q2 (`quartile <= 2`).
- **Revues non classées** (absentes de Scimago, `quartile IS NULL`) : quand un
  filtre quartile est actif, elles sont **écartées** (choix conservateur) ;
  sans filtre, elles sont conservées.
- **Normalisation ISSN** : Scimago fournit les ISSN sans tiret et parfois deux
  par revue collés. Il faut normaliser (retirer tirets/espaces) et indexer
  **chaque** ISSN d'une revue, car PubMed renvoie tantôt l'ISSN papier, tantôt
  l'électronique. La jointure article↔qualité se fait sur ISSN normalisé.

## 8. Interface (FastAPI + Jinja2 + HTMX)

### Structure
- **Sidebar** : dossiers → requêtes, chacune avec un compteur de non-lus.
- **Panneau principal** : au clic sur une requête, HTMX charge la liste
  d'articles (fragment) depuis la base. Chaque ligne : titre, revue + **badge
  quartile**, date, résumé dépliable (efetch à la demande), lien PubMed, bouton
  « marquer lu ». Les articles nouveaux depuis la dernière visite sont surlignés.

### Écran de création / édition de requête
- **Zone de texte** pour la chaîne PubMed (mode écrit).
- **Champs qualité séparés** : `quartile_min` (menu), `citedness_min` (nombre).
- **Aperçu live** : à chaque modification (anti-rebond ~500 ms), appel `esearch`
  → afficher le **nombre de résultats**, la **`QueryTranslation`** et les
  avertissements. C'est la validation déléguée à PubMed.
- **Liens aller-retour** :
  - « Ouvrir dans PubMed » → `https://pubmed.ncbi.nlm.nih.gov/?term=<url-encodé>`
    (l'utilisateur y affine dans le builder officiel puis recolle).
- **Résolveur de nom de revue** : autocomplétion via `db=nlmcatalog` pour
  obtenir l'abréviation officielle (piège NEJM → `N Engl J Med`).
- **Pas de constructeur visuel à clics.**

### Endpoints (indicatif)
- `GET /` : coquille de page.
- `GET /queries` : fragment sidebar (dossiers + compteurs).
- `GET /queries/{id}/articles` : fragment liste d'articles (lecture base).
- `POST /queries/{id}/refresh` : rafraîchissement à chaud d'une requête.
- `POST /articles/{pmid}/read` : marquer lu.
- `GET /articles/{pmid}/abstract` : efetch à la demande + cache.
- `GET|POST /queries/new`, `GET|POST /queries/{id}/edit` : formulaire de création.
- `GET /preview?term=<...>` : fragment d'aperçu live (esearch).

- `POST /articles/{pmid}/rating` : note 1–3 dans une requête (`query_id`),
  0 la retire.

Tous les échanges renvoient des **fragments HTML** échangés par HTMX.

### Couche pertinence (si `ENABLE_RELEVANCE`)
- **Encart de lecture** : trois boutons « hors sujet / secondaire /
  pertinent », propres à la requête ouverte ; re-cliquer la note active la
  retire. Sous eux, le voisin qui explique la prédiction (« proche de :
  <titre>, noté 3 »).
- **Ligne d'article** : badge de la note, ou de la prédiction (« ≈ 2,6 »)
  pour un article non noté.
- **En-tête de liste** : jauge « Apprentissage · n/8 notes » tant que le
  modèle n'est pas prêt ; « N masqués · afficher » (`?show_hidden=1`) quand
  un seuil masque des articles, les lignes réaffichées étant atténuées.
- **Formulaire de requête** :
  - tri « Pertinence » : la note d'abord, la prédiction à défaut, puis la
    date ; sans note ni prédiction, c'est le tri par date ;
  - champ séparé « Masquer les peu pertinents » (rien, < 1,5, < 2, < 2,5) :
    inactif tant que le modèle apprend, toujours réversible une fois posé ;
  - bloc repliable « Affiner la chaîne d'après tes notes » : termes qui
    distinguent les titres notés 3 des titres notés 1 (log-odds lissé, au
    moins deux titres du côté suggéré), en fragments prêts à copier
    (`AND "x"[tiab]`, `NOT "y"[tiab]`). Rien n'est inséré dans la chaîne.
- **Masquage** : un article est masqué si sa note, ou à défaut sa prédiction,
  est sous le seuil ; sans note ni prédiction, il n'est jamais masqué. Une
  seule expression SQL décide du masquage pour la liste, son comptage, les
  non-lus de la barre latérale et le flux des nouveautés, pour que les
  compteurs ne contredisent pas la liste. « Tout marquer comme lu » ne touche
  que ce qui est affiché.

## 9. Critères d'acceptation

Deux requêtes de référence servent de tests de bout en bout :

- **Requête A — précise + qualité** : une requête MeSH/`[tiab]` sur un thème
  large, avec `quartile_min = 1`. *Attendu* : seuls des articles de revues Q1
  apparaissent ; aucune revue de quartile > 1 ni non classée.
- **Requête B — NEJM / RCT** : `"N Engl J Med"[ta] AND randomized controlled
  trial[pt]`, sans filtre qualité. *Attendu* : uniquement des articles de
  *N Engl J Med*, tous de type essai randomisé.

Avec la couche pertinence, notation et masquage actifs, A ne montre toujours
que du Q1 et B que du *N Engl J Med* ; retirer le seuil de masquage rend le
total d'avant.

## 10. Découpage en phases suggéré

1. Schéma SQLite (WAL) + script d'import du CSV Scimago dans `journal_quality`.
2. `refresh_query` sur une requête écrite en dur, résultats affichés en console
   (valider tout le pipeline avant toute UI).
3. Coquille FastAPI + sidebar + liste d'articles en lecture base (HTMX).
4. Écran de création/édition + aperçu live + liens PubMed + résolveur de revue.
5. Résumé à la demande, marquage lu, surlignage des nouveaux.
6. Job de fond APScheduler + compteurs de non-lus.

Chaque phase se termine par un commit et une vérification (idéalement contre les
requêtes A et B dès qu'elles sont exécutables).

## 11. Hors périmètre

- Pas de constructeur de requête visuel à clics.
- Pas de parsing/validation locale de la syntaxe PubMed.
- Pas d'application ou d'agrégateur tiers.
- Pas de mélange des filtres qualité dans la chaîne PubMed.
- Pas d'insertion automatique des termes suggérés par la pertinence dans la
  chaîne PubMed.
- Pas d'authentification multi-utilisateur (usage personnel, réseau local).
