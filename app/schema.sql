-- Schéma de PubMed Veille. Idempotent : rejouable sans effet de bord.
-- Toutes les dates sont des chaînes ISO 8601 (UTC).

-- Requêtes PubMed sauvegardées, organisées en dossiers.
CREATE TABLE IF NOT EXISTS queries (
    id                INTEGER PRIMARY KEY,
    name              TEXT    NOT NULL,
    folder            TEXT    NOT NULL DEFAULT 'Sans dossier',
    -- Chaîne PubMed brute : source de vérité, jamais réécrite ni normalisée.
    pubmed_query      TEXT    NOT NULL,
    -- Filtres qualité : champs séparés, appliqués après récupération.
    -- Jamais injectés dans pubmed_query.
    quartile_min      INTEGER,
    citedness_min     REAL,
    -- Ancienneté maximale en années, sur la date de publication. NULL = aucune
    -- limite. Filtre local, jamais injecté dans pubmed_query.
    max_age_years     INTEGER,
    sort              TEXT    NOT NULL DEFAULT 'date_desc',
    created_at        TEXT    NOT NULL,
    last_refreshed_at TEXT,
    last_visited_at   TEXT,
    previous_visit_at TEXT
);
-- Suivi de consultation, sur les deux dernières colonnes ci-dessus :
-- `previous_visit_at` est le repère du badge « nouveau » — un article l'est si
-- son `first_seen` lui est postérieur — et `last_visited_at` retient la dernière
-- ouverture. Deux colonnes et non une, pour qu'un simple rechargement de la
-- liste n'efface pas les badges qu'on est en train de regarder : le repère ne
-- se décale qu'après une vraie interruption (voir VISIT_SESSION_GAP_MINUTES).
--
-- Le commentaire est ici et non dans la table : SQLite conserve le texte du
-- CREATE TABLE tel quel, et un commentaire attaché à la dernière colonne fait
-- échouer tout futur ALTER TABLE ... DROP COLUMN, la parenthèse fermante se
-- retrouvant commentée.

-- Articles récupérés, tous requêtes confondues.
CREATE TABLE IF NOT EXISTS articles (
    pmid       TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    -- Rempli à la demande par efetch, quand l'utilisateur déplie l'article.
    abstract   TEXT,
    journal    TEXT,
    -- ESummary expose `issn` (papier) et `essn` (électronique) ; on garde les
    -- deux, normalisés, car Scimago n'indexe pas toujours le même.
    issn       TEXT,
    eissn      TEXT,
    pub_date   TEXT,
    -- DOI tel que renvoyé par ESummary, sans préfixe ni URL. '' = interrogé
    -- sans résultat ; NULL = ligne antérieure à l'ajout de la colonne.
    doi        TEXT,
    -- Métadonnées de citation, remplies à la demande quand l'article est mis
    -- en favori. Même convention que `doi` : '' = interrogé, rien à dire.
    authors      TEXT,  -- noms ESummary (« Doe J »), joints par « ; »
    journal_full TEXT,  -- titre complet de la revue, pour la bibliographie
    volume       TEXT,
    issue        TEXT,
    pages        TEXT,
    url        TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    -- Résumé français produit à la demande par le LLM local. NULL = jamais
    -- généré. Le modèle et l'horodatage disent quoi regénérer si l'on change
    -- de modèle. Pas de commentaire sur la dernière colonne : voir plus haut.
    summary_fr    TEXT,
    summary_model TEXT,
    summary_at    TEXT
);

-- Appartenance d'un article à une requête, avec le suivi de lecture.
CREATE TABLE IF NOT EXISTS query_articles (
    query_id   INTEGER NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
    pmid       TEXT    NOT NULL REFERENCES articles(pmid) ON DELETE CASCADE,
    first_seen TEXT    NOT NULL,
    read       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (query_id, pmid)
);

-- Qualité par revue. Une ligne PAR ISSN : une même revue apparaît autant de
-- fois qu'elle a d'ISSN, pour que la jointure aboutisse quel que soit celui
-- que PubMed a renvoyé.
CREATE TABLE IF NOT EXISTS journal_quality (
    issn         TEXT PRIMARY KEY,
    journal_name TEXT,
    quartile     INTEGER,  -- 1 à 4, « SJR Best Quartile ». NULL = non classée.
    sjr          REAL,
    citedness    REAL,     -- OpenAlex, 2yr_mean_citedness.
    -- Source ayant alimenté la ligne en dernier. Scimago fournit quartile/sjr,
    -- OpenAlex fournit citedness : chacun met à jour ses colonnes sans écraser
    -- celles de l'autre.
    source       TEXT,
    updated_at   TEXT NOT NULL
);

-- Favoris : la bibliographie personnelle. Global à l'article et non lié à une
-- requête, pour que la liste reste unique quelle que soit la requête d'origine.
CREATE TABLE IF NOT EXISTS favorites (
    pmid     TEXT PRIMARY KEY REFERENCES articles(pmid) ON DELETE CASCADE,
    added_at TEXT NOT NULL
);

-- État de l'application, en clé/valeur. Sert à retenir l'horodatage de la
-- dernière ouverture : les articles apparus après celle-ci sont « nouveaux ».
CREATE TABLE IF NOT EXISTS app_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Recherche plein texte sur le cache local. `content='articles'` : l'index ne
-- duplique pas le texte, il pointe vers la table source. Les résumés n'étant
-- récupérés qu'à la demande (§6 du spec), l'index porte surtout sur les
-- titres, les revues et les auteurs, et s'enrichit au fil des lectures.
CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    title,
    abstract,
    journal_full,
    authors,
    content='articles',
    content_rowid='rowid',
    -- Sans diacritiques : « pédiatrie » doit se trouver en tapant « pediatrie ».
    tokenize="unicode61 remove_diacritics 2"
);

-- Synchronisation de l'index. En contenu externe, une suppression s'écrit
-- comme une insertion de la commande 'delete' portant les ANCIENNES valeurs :
-- FTS5 en a besoin pour retrouver les entrées à retirer.
CREATE TRIGGER IF NOT EXISTS articles_fts_insert AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts(rowid, title, abstract, journal_full, authors)
    VALUES (new.rowid, new.title, new.abstract, new.journal_full, new.authors);
END;

CREATE TRIGGER IF NOT EXISTS articles_fts_delete AFTER DELETE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, abstract, journal_full, authors)
    VALUES ('delete', old.rowid, old.title, old.abstract, old.journal_full, old.authors);
END;

CREATE TRIGGER IF NOT EXISTS articles_fts_update AFTER UPDATE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, abstract, journal_full, authors)
    VALUES ('delete', old.rowid, old.title, old.abstract, old.journal_full, old.authors);
    INSERT INTO articles_fts(rowid, title, abstract, journal_full, authors)
    VALUES (new.rowid, new.title, new.abstract, new.journal_full, new.authors);
END;

CREATE INDEX IF NOT EXISTS idx_queries_folder        ON queries(folder);
CREATE INDEX IF NOT EXISTS idx_articles_issn         ON articles(issn);
CREATE INDEX IF NOT EXISTS idx_articles_eissn        ON articles(eissn);
CREATE INDEX IF NOT EXISTS idx_qa_query_read         ON query_articles(query_id, read);
CREATE INDEX IF NOT EXISTS idx_qa_pmid               ON query_articles(pmid);
