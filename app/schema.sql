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
    last_refreshed_at TEXT
);

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
    url        TEXT NOT NULL,
    fetched_at TEXT NOT NULL
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

-- État de l'application, en clé/valeur. Sert à retenir l'horodatage de la
-- dernière ouverture : les articles apparus après celle-ci sont « nouveaux ».
CREATE TABLE IF NOT EXISTS app_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_queries_folder        ON queries(folder);
CREATE INDEX IF NOT EXISTS idx_articles_issn         ON articles(issn);
CREATE INDEX IF NOT EXISTS idx_articles_eissn        ON articles(eissn);
CREATE INDEX IF NOT EXISTS idx_qa_query_read         ON query_articles(query_id, read);
CREATE INDEX IF NOT EXISTS idx_qa_pmid               ON query_articles(pmid);
