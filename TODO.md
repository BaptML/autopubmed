# TODO de développement

Idées validées en discussion mais pas encore planifiées. Chaque point suit le
cycle de `CLAUDE.md` : plan approuvé d'abord, une phase et un commit par point.

## Pertinence (`app/relevance.py`)

Avant de commencer : mesurer le modèle sur une requête bien notée, avec
`uv run python -m app.relevance --id N` (erreur moyenne contre la référence
« moyenne seule »). Le résultat dit si le point 3 vaut l'effort.

1. **Ne pas masquer un article sans voisin ressemblant.**
   Un article qui ne partage aucun terme avec les articles notés reçoit la note
   moyenne de la requête (`Model.predict`). Si cette moyenne est sous le seuil
   `hide_below`, tous les sujets inédits sont masqués. Correction : pas de
   prédiction (donc article visible) quand la meilleure similarité est sous un
   plancher. Environ 10 lignes, sans migration.

2. **Montrer la fiabilité du modèle dans l'interface.**
   Le masquage se débloque à 8 notes, même si le modèle ne bat pas la référence.
   `leave_one_out` calcule déjà l'erreur ; l'afficher près du menu « Masquer les
   peu pertinents » (`_query_form.html`) et prévenir quand le modèle ne fait pas
   mieux que la moyenne seule. Sans migration.

3. **Ajouter le type de publication comme terme.**
   ESummary renvoie `pubtype` (essai randomisé, revue, éditorial…), déjà dans la
   réponse récupérée mais non lu (`app/pubmed.py`). Le stocker et l'ajouter dans
   `features`, comme la revue. Demande une colonne, donc une migration séparée ;
   les articles déjà en base ne l'auront qu'après un nouveau passage.

Écarté : les synonymes (MeSH ou modèle de langue). MeSH demanderait un `efetch`
à chaque rafraîchissement, contraire à la règle « efetch à la demande ».

## Restes de la revue de code

4. **Citedness des articles déjà rattachés.**
   Si un seuil de citedness est ajouté après coup, un article ancien dont la
   revue n'a jamais été interrogée sur OpenAlex reste masqué (limite notée dans
   `spec.md` §7). Correction : interroger aussi les revues des articles déjà
   rattachés dans `_fill_citedness` (`app/refresh.py`). Environ 10 lignes.

5. **Vérification automatique sur GitHub.**
   Un workflow qui lance `uv run ruff check . && uv run ruff format --check . &&
   uv run pytest` à chaque push.

6. **Port Docker.**
   `docker-compose.yml` ouvre le port 8005 à tout le réseau, sans mot de passe
   (choix du spec §11). Si l'appli n'est utilisée que depuis le serveur :
   `"127.0.0.1:8005:8000"`.

7. **Image `uv:0.11.32` du `Dockerfile`.**
   Version figée sans que son existence ait été vérifiée : à confirmer au
   premier build sur le serveur.
