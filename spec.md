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
| max_age_years | INTEGER NULL | ancienneté maximale, sur la date de publication ; NULL = aucune limite (voir §6) |
| sort | TEXT | tri d'affichage : `date_desc`, `date_asc`, `journal`, `relevance` |
| created_at | TEXT | ISO 8601 |
| last_refreshed_at | TEXT NULL | ISO 8601 |
| last_visited_at | TEXT NULL | ISO 8601 — dernière ouverture de la requête |
| previous_visit_at | TEXT NULL | ISO 8601 — repère du badge « nouveau » (voir §8) |

`max_age_years` est, comme les filtres qualité, un champ séparé : il n'est
jamais injecté dans `pubmed_query`.

### `articles`
| champ | type | note |
|---|---|---|
| pmid | TEXT PK | |
| title | TEXT | |
| abstract | TEXT NULL | rempli à la demande (voir §6) ; `''` = article sans résumé |
| journal | TEXT | abréviation NLM |
| issn | TEXT NULL | ISSN papier normalisé (voir §7) |
| eissn | TEXT NULL | ISSN électronique normalisé |
| pub_date | TEXT | ISO 8601, éventuellement partielle (« 2026 », « 2026-03 ») |
| doi | TEXT NULL | sans préfixe ni URL |
| authors | TEXT NULL | noms ESummary (« Doe J »), joints par « ; » |
| journal_full | TEXT NULL | titre complet de la revue, pour la bibliographie |
| volume, issue, pages | TEXT NULL | métadonnées de citation |
| url | TEXT | lien PubMed |
| fetched_at | TEXT | ISO 8601 |
| summary_fr | TEXT NULL | résumé français du LLM local ; NULL = jamais généré |
| summary_model | TEXT NULL | modèle ayant produit `summary_fr` |
| summary_at | TEXT NULL | ISO 8601 — date de génération |

Pour `doi` et les métadonnées de citation, `''` signifie « interrogé, rien à
dire » et NULL « jamais interrogé » : seul NULL déclenche un nouvel appel.

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

### `favorites`
| champ | type | note |
|---|---|---|
| pmid | TEXT PK | FK articles (`ON DELETE CASCADE`) |
| added_at | TEXT | ISO 8601 |

La bibliographie personnelle. Globale à l'article et non liée à une requête :
la liste reste unique quelle que soit la requête d'origine, et un favori
survit à la suppression de la requête qui l'avait ramené.

### `app_state`
| champ | type | note |
|---|---|---|
| key | TEXT PK | |
| value | TEXT | |
| updated_at | TEXT | ISO 8601 |

État de l'application en clé/valeur. Il sert aujourd'hui à tracer les
nettoyages qui ne doivent être joués qu'une fois au démarrage (clé
`future_articles_purged` : retrait des articles datés après aujourd'hui).

### `articles_fts` (index plein texte)
Table virtuelle FTS5 sur `title`, `abstract`, `journal_full` et `authors`, en
contenu externe (`content='articles'` : le texte n'est pas dupliqué), avec le
tokenizer `unicode61 remove_diacritics 2` (« pediatrie » trouve
« pédiatrie »). Trois triggers sur `articles` la tiennent à jour. Les résumés
n'étant récupérés qu'à la demande, l'index porte surtout sur titres, revues et
auteurs, et s'enrichit au fil des lectures.

### Migrations
`schema.sql` est idempotent (`CREATE ... IF NOT EXISTS`). Les colonnes ajoutées
après coup à `queries` et `articles` sont rattrapées au démarrage par
`ALTER TABLE ... ADD COLUMN` (`app/db.py`) ; l'index plein texte est construit
s'il manque.

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
  `source` (abréviation), `issn`/`essn`, `pubdate`, DOI, auteurs, volume,
  numéro, pages. **Métadonnées légères, sans résumé.**
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

### Ollama — résumé français (LLM local, optionnel)
- Service Ollama tournant **sur la même machine** : aucune donnée ne sort du
  serveur, pas de clé d'API. Il ne fait que reformuler un abstract déjà en
  base ; ce n'est pas une source bibliographique.
- `POST {OLLAMA_BASE_URL}/api/chat`, `stream: false`, `think: false` (les
  modèles de raisonnement ne brûlent pas de tokens avant de répondre),
  `num_predict` = 700, `num_ctx` = `OLLAMA_NUM_CTX`, `keep_alive` =
  `OLLAMA_KEEP_ALIVE`.
- Prompt système maison (`app/llm.py`, température 0,2) : un paragraphe de 3 à
  4 phrases en français, chiffres recopiés tels quels, rien d'absent du texte.
  Un modèle construit par `ollama create` avec une directive `SYSTEM` est
  détecté (`/api/show`) et garde son propre prompt.
- Une seule génération à la fois (sémaphore), pour ne pas faire déborder la
  VRAM.
- Banc d'essai : `uv run python -m app.llm [--model m1,m2] [PMID]`.

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
   Avant la qualité, deux filtres de date écartent les articles trop anciens
   pour `max_age_years` et ceux datés après aujourd'hui (voir plus bas).
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
Un article sans résumé (éditorial, lettre) est mis en cache comme `''`. Le
même modèle vaut pour le résumé français (Ollama, à la demande, voir §8) et
pour les métadonnées de citation des favoris (`esummary`, en un seul lot, à la
pose d'un favori ou à l'ouverture de la bibliographie).

**Filtre d'ancienneté** (`max_age_years`, 1, 3 ou 5 ans, ou aucune limite) :
fenêtre glissante sur la date de publication (N années retranchées à
aujourd'hui, pas des années civiles). Il s'applique deux fois : au
rafraîchissement (l'article trop ancien n'est pas rattaché) et à la lecture
(la liste, ses compteurs et le flux des nouveautés le réappliquent, si bien
qu'un article vieillit hors de la liste sans nouveau rafraîchissement).
Les dates partielles de PubMed sont complétées vers la fin de période
(« 2021 » → `2021-12-31`) avant comparaison ; un article sans date est gardé.

**Articles datés dans le futur** (numéros « à paraître », coquilles PubMed) :
écartés au rafraîchissement et masqués à la lecture (listes, flux, recherche),
avec la même complétion vers la fin de période. Un nettoyage unique, tracé
dans `app_state`, a retiré ceux déjà en base ; un favori garde sa ligne mais
perd ses rattachements aux requêtes.

## 7. Détails du filtrage qualité

- **Quartile** : on utilise le « SJR Best Quartile » (meilleur quartile toutes
  catégories). Stocké en entier 1–4. Filtre `quartile_min = 2` → on garde Q1 et
  Q2 (`quartile <= 2`).
- **Revues non classées** (absentes de Scimago, `quartile IS NULL`) : quand un
  filtre quartile est actif, elles sont **écartées** (choix conservateur) ;
  sans filtre, elles sont conservées.
- **Réappliqué à la lecture** : comme l'ancienneté, les filtres qualité
  s'appliquent deux fois — au rafraîchissement (l'article n'est pas rattaché)
  et à l'affichage (liste, compteurs, non-lus, flux des nouveautés). Durcir un
  filtre après coup vaut donc aussi pour les articles déjà rattachés ; le
  relâcher les fait réapparaître, rien n'est supprimé. La citedness d'une
  revue n'étant récupérée qu'au rafraîchissement d'une requête qui filtre
  dessus, un article ancien d'une revue jamais interrogée reste masqué tant
  qu'un rafraîchissement ne l'a pas ramenée.
- **Normalisation ISSN** : Scimago fournit les ISSN sans tiret et parfois deux
  par revue collés. Il faut normaliser (retirer tirets/espaces) et indexer
  **chaque** ISSN d'une revue, car PubMed renvoie tantôt l'ISSN papier, tantôt
  l'électronique. La jointure article↔qualité se fait sur ISSN normalisé.

## 8. Interface (FastAPI + Jinja2 + HTMX)

### Structure
- **Sidebar** : en tête, le champ de recherche dans le cache, puis deux entrées
  qui traversent tous les dossiers — « Nouveautés » (avec son compteur) et
  « Favoris » ; ensuite dossiers → requêtes, chacune avec un compteur de
  non-lus.
- **Panneau principal** : au clic sur une requête, HTMX charge la liste
  d'articles (fragment) depuis la base, par pages de 50 (« charger la suite »).
  Chaque ligne : titre, revue + **badge quartile**, date, lien PubMed, bouton
  « marquer lu ». L'en-tête rappelle les filtres (dont l'ancienneté) et offre
  « favoris seulement » et « Tout marquer comme lu » (cette requête seule).
- **Encart de lecture** : ouvrir un article le marque lu et affiche son résumé
  (efetch à la demande), le résumé français s'il est activé, les boutons
  « marquer lu / non lu » et « favori ».
- **Badge « nouveau »** : un article l'est si son `first_seen` est postérieur
  au `previous_visit_at` de la requête. Ouvrir la requête fait avancer ce
  repère, mais seulement après une interruption d'au moins
  `VISIT_SESSION_GAP_MINUTES` (défaut 30) : recharger la liste n'efface pas
  les badges qu'on est en train de regarder. À la première ouverture, le
  repère tombe sur la création de la requête.

### Écran de création / édition de requête
- **Zone de texte** pour la chaîne PubMed (mode écrit).
- **Champs qualité séparés** : `quartile_min` (menu), `citedness_min` (nombre).
- **Ancienneté maximale** : `max_age_years` (menu : pas de limite, 1, 3 ou
  5 ans), champ séparé lui aussi.
- **Tri d'affichage** : date décroissante, date croissante, revue (et « Pertinence »,
  voir plus bas).
- **Aperçu live** : à chaque modification (anti-rebond ~500 ms), appel `esearch`
  → afficher le **nombre de résultats**, la **`QueryTranslation`** et les
  avertissements. C'est la validation déléguée à PubMed.
- **Liens aller-retour** :
  - « Ouvrir dans PubMed » → `https://pubmed.ncbi.nlm.nih.gov/?term=<url-encodé>`
    (l'utilisateur y affine dans le builder officiel puis recolle).
- **Résolveur de nom de revue** : autocomplétion via `db=nlmcatalog` pour
  obtenir l'abréviation officielle (piège NEJM → `N Engl J Med`).
- **Pas de constructeur visuel à clics.**

### Flux des nouveautés
- Tous les articles « nouveaux » au sens du badge, toutes requêtes
  confondues, du plus récemment rattaché au plus ancien, avec le nom de la
  requête d'origine. Un article présent dans deux requêtes y figure une fois
  par requête : lecture et repère se suivent par paire (requête, article).
- Chaque requête y garde son propre `max_age_years` ; les articles datés dans
  le futur en sont exclus.
- **Consulter le flux ne le vide pas.** Une nouveauté disparaît quand on ouvre
  sa requête (repère avancé), ou par « Tout marquer comme vu », qui cale le
  repère de toutes les requêtes sur maintenant **sans** marquer lu.
- Une seule clause SQL définit « nouveau » pour le badge, le flux et son
  compteur, qui ne peuvent donc pas se contredire.

### Recherche dans le cache
- Champ de recherche en tête de sidebar (anti-rebond ~400 ms), sur l'index
  `articles_fts` : titres, résumés déjà récupérés, revues, auteurs. **Aucun
  appel réseau** : c'est la base locale, pas PubMed.
- La saisie libre est convertie en expression FTS5 sûre : chaque terme est mis
  entre guillemets (littéral), les termes sont joints par un ET implicite, le
  dernier reçoit un joker de préfixe s'il fait plus d'une lettre.
- Résultats classés par pertinence FTS puis date, un article par ligne quelle
  que soit la requête, paginés par 50 ; les articles datés dans le futur sont
  exclus.

### Favoris et bibliographie
- Bouton « favori » sur la ligne et dans l'encart ; le favori est global à
  l'article (table `favorites`).
- Poser un favori ou ouvrir la bibliographie complète en un seul lot
  `esummary` les métadonnées de citation manquantes (auteurs, revue complète,
  volume, numéro, pages, DOI). Un échec réseau reste silencieux : le favori
  est posé, la bibliographie s'affiche avec ce qu'on a.
- Vue « Favoris » : tous les favoris, du plus récemment ajouté au plus ancien,
  avec leur rendu au style **Vancouver** et un export **BibTeX**
  (`favoris.bib`) pour Zotero, Mendeley ou LaTeX.
- Dans une requête, « favoris seulement » restreint la liste aux favoris.

### Résumé français (si `ENABLE_LLM_SUMMARY`, défaut 1)
- Bloc « Résumé en français » dans l'encart de lecture : un bouton lance la
  génération par Ollama (voir §5) ; le résultat est mis en cache dans
  `articles.summary_fr` avec le modèle et la date, et s'affiche directement aux
  ouvertures suivantes. Régénérer se fait sur demande explicite.
- L'abstract est chargé (efetch) s'il n'est pas en cache ; sans abstract, ou si
  PubMed ou le modèle ne répondent pas, un message le dit.
- Ollama n'est pas sondé au démarrage : un service lancé après l'application
  reste utilisable sans redémarrer.
- `ENABLE_LLM_SUMMARY=0` retire le bloc de l'interface.
- Réglages `.env` : `OLLAMA_BASE_URL`, `OLLAMA_MODEL`,
  `OLLAMA_TIMEOUT_SECONDS`, `OLLAMA_NUM_CTX`, `OLLAMA_KEEP_ALIVE`.

### Endpoints (indicatif)
- `GET /` : coquille de page.
- `GET /queries` : fragment sidebar (dossiers + compteurs).
- `GET /queries/{id}/articles` : fragment liste d'articles (lecture base) ;
  `favorites=1`, `offset` pour les pages suivantes.
- `POST /queries/{id}/refresh` : rafraîchissement à chaud d'une requête.
- `POST /queries/{id}/read-all` : tout marquer comme lu (cette requête).
- `DELETE /queries/{id}` : supprimer une requête.
- `POST /articles/{pmid}/read` : marquer lu / non lu.
- `GET /articles/{pmid}/panel?query_id=<id>` : encart de lecture ; marque lu,
  efetch du résumé à la demande + cache.
- `POST /articles/{pmid}/favorite` : poser / retirer un favori.
- `POST /articles/{pmid}/summary` : résumé français par le LLM local
  (`regenerate=1` pour forcer).
- `GET|POST /queries/new`, `GET|POST /queries/{id}/edit` : formulaire de création.
- `GET /preview?pubmed_query=<...>` : fragment d'aperçu live (esearch).
- `GET /journals/resolve?q=<...>` : résolveur de nom de revue (nlmcatalog).
- `GET /new` : flux des nouveautés ; `POST /new/dismiss` : tout marquer comme
  vu.
- `GET /search?q=<...>` : recherche dans le cache local.
- `GET /favorites` : bibliographie ; `GET /favorites/export.bib` : export
  BibTeX.

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
