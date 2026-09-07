# Handoff design : AutoPubMed

> Référence de design historique. Ces écrans ont été implémentés à la phase 7
> (socle visuel Industry) ; ce dossier reste comme trace de l'intention.

## Overview
Application web personnelle de veille bibliographique. L'utilisateur enregistre des requêtes PubMed avancées ; chaque requête a une page qui affiche « l'état du jour » : les articles indexés qui y répondent, groupés par jour, avec un encart de lecture pour prévisualiser le résumé. Usage mono-utilisateur, ~10 à 40 articles par jour, interface en français, contenu des articles en anglais (texte source PubMed).

## About the Design Files
Les fichiers de ce dossier sont des **références de design réalisées en HTML** — un prototype qui montre l'apparence et le comportement attendus, pas du code de production à copier tel quel. Le travail consiste à **recréer ces écrans dans l'environnement existant du projet cible** (React, Vue, Svelte, SwiftUI…) avec ses conventions et ses bibliothèques. Si aucun environnement n'existe encore, choisir le framework le plus adapté (React + Vite convient bien à cette interface) et y implémenter les écrans.

`prototype.dc.html` est un composant à template + classe de logique ; les données d'articles y sont en dur (`ARTICLES`, `QUERIES`) et doivent être remplacées par l'API E-utilities de PubMed (voir « Data »).

## Fidelity
**Hi-fi.** Couleurs, typographie, échelle d'espacement et interactions sont définitives. Recréer l'UI au pixel en réutilisant les tokens fournis (`industry.css`).

## Screens / Views

### 1. Coquille d'application (présente sur tous les écrans)
- **Barre supérieure**, `position: sticky; top: 0`, hauteur **54px**, fond `--color-bg`, bordure basse `1px solid color-mix(in srgb, var(--color-text) 22%, transparent)`, `padding: 0 17px`, flex, `gap: 13.6px`.
  - Bouton de repli de la barre latérale : label `⟨⟨` (ouverte) / `⟩⟩` (fermée), IBM Plex Mono 11px, `padding: 6px 8px`, bordure `--color-divider`, fond `color-mix(in srgb, var(--color-accent) 12%, transparent)` quand ouverte.
  - Logo en deux blocs collés : « AUTO » en Spectral 600, 24px, `letter-spacing: -0.02em`, majuscules, texte `--color-bg` sur fond `--color-accent-900` (#1d2d3d), `padding: 3px 9px` ; « PUBMED » même type, couleur `--color-accent-700` (#416180), bordure `1px solid --color-text` sans bord gauche.
  - « Veille quotidienne » : IBM Plex Mono 9px, majuscules, `letter-spacing: 0.06em`, texte à 55% d'opacité.
  - Poussée à droite : « Synchronisé aujourd'hui 06:15 » (même style mono 9px) puis bouton **Masquer / Afficher l'encart** (mono 9px, `padding: 7px 11px`, bordure `--color-divider`, fond accent 12% quand actif).
- **Barre latérale gauche** (pliable), `width: 232px`, `position: sticky; top: 54px`, `height: calc(100vh - 54px)`, bordure droite 22% d'encre, colonne flex :
  - Titre « MES REQUÊTES » mono 9px 50% opacité, `padding: 0 17px 10.2px`.
  - Liste des requêtes : un bouton par requête, `width: 100%`, `padding: 10.2px 17px`, `border-left: 3px solid` (accent si active, transparent sinon), fond `color-mix(in srgb, var(--color-accent) 10%, transparent)` si active. Ligne 1 : libellé 13.5px `line-height: 1.3` + badge mono 9px à droite (« 6 nouv. » en `--color-accent-700`, sinon « à jour » en 40% d'encre). Ligne 2 : méta mono 8.5px 45% d'encre — cadence + total (« quotidien · 06:15 · 1 284 au total »).
  - Bouton « + Nouvelle requête » : mono 9px, bordure `--color-divider`, `padding: 7px 10.2px`, marge `13.6px 17px 0`, texte `--color-accent-700` → ouvre l'éditeur.
  - Pied de barre : « VOLUME · 7 JOURS », histogramme de 7 barres, hauteur de zone 52px, barres `flex: 1`, hauteur = ratio × 40px, `--color-accent-300` sauf le jour courant en `--color-accent`, étiquette mono 8px sous chaque barre.

### 2. Page d'une requête — état du jour (écran principal)
- **En-tête de requête**, `position: sticky; top: 54px`, `padding: 20.4px 27.2px 13.6px`, bordure basse 22% :
  - Sur-titre mono 9px `--color-accent-700` : « ÉTAT DU JOUR · mardi 18 août 2026 ».
  - `h1` = nom de la requête, Spectral 600, **34px**, `line-height: 1.06`, `text-wrap: pretty`.
  - À droite, deux compteurs : label mono 8.5px 50% d'encre + valeur Spectral 30px `line-height: 1.15` (« Retenus » en `--color-text`, « Non lus » en `--color-accent-700`), puis bouton `.btn .btn-secondary` « Modifier la requête ».
  - `<details>` « VOIR LA CHAÎNE PUBMED » (summary mono 9px accent-700) révélant la requête générée en 12.5px, `line-height: 1.5`, `word-break: break-word`, 75% d'encre.
- **Corps** : `display: flex; flex-wrap: wrap; gap: 27.2px; align-items: flex-start; padding: 20.4px 27.2px 68px`.
  - Colonne liste : `flex: 3 1 240px; min-width: 0`.
  - Encart de lecture : `flex: 2 1 220px; max-width: 460px; position: sticky; top: 222px` (pliable).
  - Ces bases flex sont volontairement basses : au-dessous de ~590px de zone de contenu l'encart passerait sous la liste.
- **Barre de filtres** (au-dessus de la liste, bordure basse `--color-divider`, `padding-bottom: 6.8px`) : puces « Tous / Non lus / Essais randomisés / Synthèses » — 12px, `padding: 5px 11px`, angles droits, bordure `--color-divider` ; état actif = fond `--color-accent`, texte `--color-bg`, bordure accent. À droite : `.btn .btn-ghost` 13px « Tout marquer comme lu » (n'affecte que la requête courante).
- **Groupe de jour** : `h3` Spectral 20px (« Mardi 18 août 2026 ») + filet `--color-divider` en `flex: 1` + compteur mono 9px (« 6 nouveaux » / « 4 articles »).
- **Ligne d'article** (cliquable) : flex, `gap: 13.6px`, `padding: 13.6px 13.6px 13.6px 0`, bordure basse `1px solid color-mix(in srgb, var(--color-text) 8%, transparent)`.
  - Filet vertical de 3px collé à gauche, `--color-accent` si non lu, transparent si lu.
  - Ligne méta : `.tag .tag-accent` (type d'étude) + revue (mono 9px 55%) + date (mono 9px 40%) + « IF 158.5 » (mono 9px `--color-accent-700`).
  - Titre : `h4` Spectral, **18px**, `line-height: 1.22`, `text-wrap: pretty` ; passe en `--color-accent-800` quand la ligne est sélectionnée.
  - Auteurs : 12.5px, 62% d'encre. Extrait de résumé : 13px, `line-height: 1.5`, 70% d'encre, tronqué à **2 lignes** (`-webkit-line-clamp: 2`).
  - Bouton « MARQUER LU » / « LU » à droite : mono 9px majuscules, `padding: 4px 9px`, bordure `--color-divider` (transparente une fois lu) ; `stopPropagation` pour ne pas ouvrir l'article.
  - Ligne sélectionnée : fond `color-mix(in srgb, var(--color-accent) 9%, transparent)`. Ligne lue non sélectionnée : `opacity: 0.55`.
- **Pied de liste** : note mono 9px 45% — « Archive des 90 derniers jours · 1 284 articles indexés pour cette requête ».

### 3. Encart de lecture (prévisualisation du résumé, pliable)
Cadre `.blueprint` + quatre `<i class="corner tl|tr|bl|br">`, `padding: 20.4px`, colonne flex `gap: 13.6px`, `max-height: calc(100vh - 200px)`, `overflow: auto`.
- En-tête : « LECTURE » (mono 9px accent-700) ; à droite « PMID 40918822 » (mono 9px 45%) et bouton `×` de repli (11px, bordure `--color-divider`).
- Titre `h2` Spectral 26px `line-height: 1.12` ; liste complète des auteurs 13px 70% d'encre.
- Bandeau de trois cellules (`grid-template-columns: repeat(auto-fit, minmax(120px, 1fr))`, filets haut/bas `--color-divider`, `padding: 10.2px 0`) : label mono 8.5px 50% + valeur Spectral 16px — « Revue · IF », « Effectif », « Devis ».
- « RÉSUMÉ » puis résumé complet 14px `line-height: 1.62`, `text-wrap: pretty`.
- Bloc conclusion : `border-left: 2px solid var(--color-accent)`, `padding-left: 13.6px`, label mono 9px accent-700 « CONCLUSION DES AUTEURS », texte 13.5px `line-height: 1.55`.
- « TERMES MESH » : suite de `.tag .tag-outline`, `gap: 5px`.
- Pied : `.btn .btn-primary` « Marquer comme lu / non lu » (`flex: 1 1 160px`) + `.btn .btn-secondary` lien « Voir sur PubMed » (`flex: 1 1 140px`, `target="_blank"`, `https://pubmed.ncbi.nlm.nih.gov/<pmid>/`).

### 4. Éditeur de requête avancée
Panneau `.blueprint` (`padding: 20.4px`, `flex: 3 1 480px`) + colonne latérale droite (`flex: 2 1 340px`, `max-width: 500px`, `position: sticky; top: 20.4px`).
- Titre `h2` Spectral 28px, sous-titre 13px 62% d'encre.
- **Blocs de requête** : grille `minmax(64px, 92px) minmax(120px, 1fr) minmax(150px, 2fr) 36px`, `gap: 6.8px`. En-têtes de colonnes mono 8.5px 50% : Opérateur / Champ / Terme.
  - Ligne 1 : libellé « Bloc 1 » à la place du sélecteur d'opérateur. Lignes suivantes : `<select class="input">` AND / OR / NOT.
  - Champ : `<select class="input">` — `tiab` Titre / Résumé, `mh` Terme MeSH, `ti` Titre, `au` Auteur, `ta` Revue, `pt` Type de publication, `all` Tous les champs.
  - Terme : `<input class="input">`, placeholder « ex. heart failure ».
  - Suppression : `.btn .btn-secondary .btn-icon` avec `×`.
  - « + Ajouter une ligne » : `.btn .btn-ghost`.
- **Dates** : deux `.field` (`Publié depuis` / `Publié jusqu'au`), format `AAAA/MM/JJ`, grille `repeat(auto-fit, minmax(180px, 1fr))`, `gap: 20.4px`.
- **Types d'article** (puces multi-sélection) : Randomized Controlled Trial, Meta-Analysis, Systematic Review, Observational Study, Guideline, Review.
- **Espèce** : Humain / Animal. **Langue** : Anglais / Français. Mêmes puces.
- **Colonne droite** : cadre « REQUÊTE GÉNÉRÉE » avec la chaîne PubMed (13px, `line-height: 1.62`), puis nombre de résultats estimés (Spectral 32px) + « résultats estimés · N / jour » (mono 8.5px) et `.btn .btn-primary` « Enregistrer et lancer ». Second cadre « APERÇU DU JOUR » avec une note explicative 13px.

## Interactions & Behavior
- Clic sur une ligne d'article : la sélectionne, la marque **lue** et remplit l'encart de lecture. L'encart ne change pas de position (sticky).
- Bouton « Marquer lu » d'une ligne : bascule lu/non lu sans changer la sélection (`stopPropagation`).
- « Tout marquer comme lu » : marque lus tous les articles **de la requête courante** uniquement.
- Filtres : purement client, sur la liste de la requête courante. Un groupe de jour vide n'est pas rendu.
- Clic sur une requête de la barre latérale : change de requête, recharge la liste, réinitialise le filtre à « Tous », charge les blocs de requête correspondants dans l'éditeur et sélectionne le premier article de la requête.
- « Modifier la requête » / « + Nouvelle requête » : passe à la vue éditeur ; « Enregistrer et lancer » revient au journal.
- La chaîne PubMed se recompose à chaque frappe / changement de puce.
- Barre latérale et encart pliables (boutons de la barre supérieure, plus le `×` de l'encart). État attendu : persistant entre sessions (localStorage).
- Aucune animation ; les transitions d'état sont instantanées. Survol : voir `industry.css` (tint accent, focus `:focus-visible` = anneau accent 2px).
- Responsive : le corps est un flex `wrap`. Sous ~900px l'encart passe sous la liste — prévoir alors de le replier par défaut ou de le présenter en tiroir droit superposé.

## State Management
- `navOpen: boolean` — barre latérale dépliée (défaut vrai).
- `panelOpen: boolean` — encart de lecture déplié (défaut vrai).
- `view: 'journal' | 'editor'`.
- `queryId: string` — requête sélectionnée.
- `selectedId: number` — article affiché dans l'encart.
- `read: Record<articleId, boolean>` — état de lecture (à persister côté serveur ou localStorage).
- `filter: 'Tous' | 'Non lus' | 'Essais randomisés' | 'Synthèses'`.
- `rows: {op, field, term}[]`, `dateFrom`, `dateTo`, `pubTypes: string[]`, `species: string[]`, `langs: string[]` — état de l'éditeur.
- Dérivés : chaîne PubMed (fonction `buildQuery`), compteurs retenus / non lus, groupes par jour, badges de la barre latérale.

### Data (à brancher)
Le prototype contient des articles fictifs. En production :
- `esearch.fcgi?db=pubmed&term=<chaîne>&sort=date&datetype=edat` pour les PMID du jour, puis `esummary.fcgi` / `efetch.fcgi?rettype=abstract` pour titre, auteurs, revue, date, résumé, MeSH.
- Le facteur d'impact n'est pas fourni par PubMed : il faut une table locale revue → IF.
- La « conclusion des auteurs » vient de la section `CONCLUSIONS` du résumé structuré quand elle existe ; sinon masquer le bloc.
- Prévoir un cron quotidien (06:15 dans la maquette) qui stocke les nouveaux PMID par requête et par jour, et un état de lecture par article.

## Design Tokens
Tous les tokens de couleur, d'espacement, de rayon et d'ombre viennent de `industry.css` (design system « Industry »). Ne pas coder de valeur en dur qu'un token porte déjà.
- Fond `--color-bg` **#f2f2f3** · surface `--color-surface` #e9e9ea · texte `--color-text` **#1d1f20** · accent `--color-accent` **#5980a6**.
- Rampe accent : 100 #eef6ff, 200 #d6ebff, 300 #b5d9fd, 400 #94bce3, 500 #749dc4, 600 #597ea3, **700 #416180** (texte accent lisible), 800 #2c455d, **900 #1d2d3d** (fond du logo).
- Rampe neutre : 100 #f5f5f8 → 900 #2b2b2d.
- Filets : structure `color-mix(in srgb, var(--color-text) 22%, transparent)` ; `--color-divider` = encre 16% ; lignes de liste = encre 8%.
- Espacement : 3.4 / 6.8 / 10.2 / 13.6 / 20.4 / 27.2px (échelle 0.85×), plus 17px et 34px utilisés comme gouttières de page.
- Typographie : titres **Spectral** 600 (Georgia en repli) ; interface **IBM Plex Sans** 400/500/600 ; étiquettes techniques **IBM Plex Mono**, majuscules, `letter-spacing: 0.06em`, 8–11px. Échelle : 34 / 30 / 28 / 26 / 20 / 18 / 16 / 14 / 13.5 / 13 / 12.5 / 12 / 9px.
- Angles : **0** partout (`border-radius: 0`) — c'est une règle du design system, y compris pour les boutons, champs et puces.
- Ombres : `--shadow-sm/md/lg` (non utilisées dans cet écran, tout est en filets).

## Assets
Aucune image. Icônes prévues : **Lucide**, `stroke-width: 1.5` (le prototype utilise du texte `⟨⟨`, `⟩⟩`, `×` en attendant). Polices : Spectral, IBM Plex Sans, IBM Plex Mono (Google Fonts).

## Files
- `prototype.dc.html` — le prototype complet (template + logique + données d'exemple). Les surcharges de police sont dans le bloc `<style>` en tête ; le reste du style est en ligne.
- `industry.css` — le design system « Industry » : tokens (`:root`) et classes de composants `.blueprint` / `.corner`, `.btn`, `.tag`, `.input`, `.field`, `.card`, `.table`, `.dialog`. Source de vérité pour les valeurs.
