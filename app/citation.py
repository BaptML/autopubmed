"""Mise en forme des favoris en bibliographie.

Fonctions pures : elles prennent les lignes d'article telles que le dépôt les
renvoie (dictionnaires) et rendent du texte. Aucun accès à la base, aucun
appel réseau — c'est ce qui les rend testables telles quelles.

Deux sorties : le style Vancouver, attendu des revues biomédicales, et le
BibTeX, pour Zotero, Mendeley et LaTeX.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

Article = Mapping[str, Any]

# Vancouver : au-delà de six auteurs, on cite les six premiers puis « et al. ».
MAX_AUTEURS_VANCOUVER = 6

# Caractères que BibTeX interprète : ils doivent être échappés dans les valeurs.
_BIBTEX_SPECIAUX = re.compile(r"([&%$#_])")


def split_authors(authors: str | None) -> list[str]:
    """Auteurs stockés (« Doe J; Roe A ») ramenés à une liste."""
    if not authors:
        return []
    return [nom.strip() for nom in authors.split(";") if nom.strip()]


def year(article: Article) -> str | None:
    """Année de publication. Les dates PubMed partielles (« 2026 ») conviennent."""
    pub_date = (article.get("pub_date") or "").strip()
    return pub_date[:4] if len(pub_date) >= 4 else None


def to_vancouver(article: Article) -> str:
    """Une référence au style Vancouver, champs manquants simplement omis."""
    morceaux: list[str] = []

    auteurs = split_authors(article.get("authors"))
    if auteurs:
        liste = ", ".join(auteurs[:MAX_AUTEURS_VANCOUVER])
        if len(auteurs) > MAX_AUTEURS_VANCOUVER:
            liste += ", et al"
        morceaux.append(f"{liste}.")

    titre = (article.get("title") or "").strip()
    if titre:
        morceaux.append(titre if titre.endswith((".", "?", "!")) else f"{titre}.")

    revue = (article.get("journal") or article.get("journal_full") or "").strip()
    if revue:
        morceaux.append(revue if revue.endswith(".") else f"{revue}.")

    if annee := year(article):
        # « 2026;390(2):128-139 » : chaque segment n'apparaît que s'il existe.
        localisation = annee
        volume = (article.get("volume") or "").strip()
        if volume:
            localisation += f";{volume}"
            if numero := (article.get("issue") or "").strip():
                localisation += f"({numero})"
        if pages := (article.get("pages") or "").strip():
            localisation += f":{pages}" if volume else f";{pages}"
        morceaux.append(f"{localisation}.")

    if doi := (article.get("doi") or "").strip():
        morceaux.append(f"doi:{doi}.")
    if pmid := (article.get("pmid") or "").strip():
        morceaux.append(f"PMID: {pmid}.")

    return " ".join(morceaux)


def to_bibliography(articles: Sequence[Article]) -> str:
    """Bibliographie numérotée, une référence par ligne."""
    return "\n".join(
        f"{rang}. {to_vancouver(article)}"
        for rang, article in enumerate(articles, start=1)
    )


def _ascii(texte: str) -> str:
    """Translittération pour les clés BibTeX, qui n'acceptent pas les accents."""
    sans_accent = unicodedata.normalize("NFKD", texte)
    return sans_accent.encode("ascii", "ignore").decode("ascii")


def _bibtex_author(nom: str) -> str:
    """« Doe J » → « Doe, J ». Les noms collectifs restent d'un seul tenant."""
    parties = nom.split()
    if len(parties) < 2:
        return nom
    *famille, initiales = parties
    # Des initiales sont courtes et tout en majuscules ; sinon c'est un nom
    # collectif (« STEP-HFpEF Trial Committees ») qu'on ne découpe pas.
    if len(initiales) > 3 or not initiales.isupper():
        return nom
    return f"{' '.join(famille)}, {initiales}"


def bibtex_key(article: Article, prises: set[str] | None = None) -> str:
    """Clé de citation « nom + année + premier mot du titre », désambiguïsée.

    `prises` accumule les clés déjà attribuées : deux articles du même auteur,
    la même année, sur le même sujet, reçoivent des suffixes a, b, c…
    """
    auteurs = split_authors(article.get("authors"))
    nom = _ascii(auteurs[0].split()[0]).lower() if auteurs else "anon"
    mots = re.findall(r"[A-Za-z]+", _ascii(article.get("title") or ""))
    mot = next((m.lower() for m in mots if len(m) > 3), "article")
    base = f"{re.sub(r'[^a-z0-9]', '', nom) or 'anon'}{year(article) or 'sd'}{mot}"
    if prises is None:
        return base
    cle, suffixe = base, ord("a")
    while cle in prises:
        cle = f"{base}{chr(suffixe)}"
        suffixe += 1
    prises.add(cle)
    return cle


def _echappe(valeur: str) -> str:
    return _BIBTEX_SPECIAUX.sub(r"\\\1", valeur)


def to_bibtex(articles: Sequence[Article]) -> str:
    """Fichier BibTeX complet, une entrée `@article` par favori."""
    prises: set[str] = set()
    entrees: list[str] = []
    for article in articles:
        champs: list[tuple[str, str]] = []
        if auteurs := split_authors(article.get("authors")):
            champs.append(
                ("author", " and ".join(_bibtex_author(nom) for nom in auteurs))
            )
        if titre := (article.get("title") or "").strip():
            # Double accolade : elle protège la casse d'origine du titre.
            champs.append(("title", "{" + _echappe(titre) + "}"))
        revue = (article.get("journal_full") or article.get("journal") or "").strip()
        if revue:
            champs.append(("journal", _echappe(revue)))
        if annee := year(article):
            champs.append(("year", annee))
        if volume := (article.get("volume") or "").strip():
            champs.append(("volume", volume))
        if numero := (article.get("issue") or "").strip():
            champs.append(("number", numero))
        if pages := (article.get("pages") or "").strip():
            champs.append(("pages", pages.replace("-", "--")))
        if doi := (article.get("doi") or "").strip():
            champs.append(("doi", _echappe(doi)))
        if pmid := (article.get("pmid") or "").strip():
            champs.append(("pmid", pmid))
            champs.append(("url", f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"))

        corps = ",\n".join(f"  {nom:<8}= {{{valeur}}}" for nom, valeur in champs)
        entrees.append(f"@article{{{bibtex_key(article, prises)},\n{corps}\n}}")
    return "\n\n".join(entrees) + ("\n" if entrees else "")
