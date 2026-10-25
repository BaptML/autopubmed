-- Couche « pertinence » : notes 1–3 par requête et prédictions qui en découlent.
-- Fichier à part de `schema.sql`, et uniquement des tables nouvelles : aucune
-- table ni colonne existante n'est touchée. Une base migrée reste donc lisible
-- par le code d'avant la fonctionnalité, et la retirer revient à supprimer ces
-- trois tables (`uv run python -m app.relevance_admin --desinstaller`).
-- Idempotent, comme `schema.sql`.

-- Notes données par l'utilisateur : 1 = hors sujet, 2 = secondaire,
-- 3 = pertinent. Par paire (requête, article) : un même article peut être
-- central dans une requête et hors sujet dans une autre.
CREATE TABLE IF NOT EXISTS article_ratings (
    query_id INTEGER NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
    pmid     TEXT    NOT NULL REFERENCES articles(pmid) ON DELETE CASCADE,
    rating   INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 3),
    rated_at TEXT    NOT NULL,
    PRIMARY KEY (query_id, pmid)
);

-- Pertinence prédite, sur l'échelle 1–3. Donnée dérivée des notes : elle se
-- recalcule entièrement, rien ne s'y perd si on la vide.
CREATE TABLE IF NOT EXISTS relevance_scores (
    query_id    INTEGER NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
    pmid        TEXT    NOT NULL REFERENCES articles(pmid) ON DELETE CASCADE,
    score       REAL    NOT NULL,
    computed_at TEXT    NOT NULL,
    PRIMARY KEY (query_id, pmid)
);

-- Réglages de la couche par requête. Table à part plutôt que colonne de
-- `queries`, pour ne pas modifier une table existante. `hide_below` NULL = rien
-- n'est masqué ; le masquage s'applique à l'affichage, jamais à l'ingestion.
CREATE TABLE IF NOT EXISTS relevance_settings (
    query_id   INTEGER PRIMARY KEY REFERENCES queries(id) ON DELETE CASCADE,
    hide_below REAL
);
