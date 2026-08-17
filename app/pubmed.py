"""Client E-utilities (NCBI).

Deux principes du projet s'appliquent ici :
- la chaîne PubMed part telle quelle, jamais réécrite ni validée localement ;
- la validation vient de la réponse d'ESearch (count, querytranslation,
  warninglist), qu'on se contente de rapporter.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from xml.etree import ElementTree

import httpx

from app.config import settings
from app.issn import normalize_issn
from app.models import ArticleSummary, ESearchResult, JournalMatch

BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# ESummary accepte de longues listes d'UID ; 200 garde les URL raisonnables.
ESUMMARY_BATCH_SIZE = 200

_SORTPUBDATE_RE = re.compile(r"^(\d{4})/(\d{2})/(\d{2})")
_PUBDATE_RE = re.compile(r"^(\d{4})(?:\s+(\w{3}))?(?:\s+(\d{1,2}))?")
_MONTHS = {
    m: f"{i:02d}"
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}


class RateLimiter:
    """Espace les appels sortants d'au moins 1/rate seconde."""

    def __init__(self, rate_per_second: float) -> None:
        self._min_interval = 1.0 / rate_per_second
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            wait = self._last_call + self._min_interval - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = loop.time()


def parse_pub_date(entry: dict[str, Any]) -> str | None:
    """Convertit une date ESummary en ISO 8601.

    `sortpubdate` (« 2015/09/17 00:00 ») est préféré car normalisé ; on
    retombe sur `pubdate` (« 2015 Sep 17 », parfois « 2015 » seul).
    """
    sortpubdate = (entry.get("sortpubdate") or "").strip()
    match = _SORTPUBDATE_RE.match(sortpubdate)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"

    match = _PUBDATE_RE.match((entry.get("pubdate") or "").strip())
    if not match:
        return None
    year, month_name, day = match.groups()
    month = _MONTHS.get((month_name or "").lower()[:3])
    if month is None:
        return year
    if day is None:
        return f"{year}-{month}"
    return f"{year}-{month}-{int(day):02d}"


def parse_esummary_entry(entry: dict[str, Any]) -> ArticleSummary | None:
    """Transforme une entrée ESummary en ArticleSummary. None si inutilisable."""
    pmid = str(entry.get("uid") or "").strip()
    if not pmid or entry.get("error"):
        return None
    return ArticleSummary(
        pmid=pmid,
        title=(entry.get("title") or "").strip() or f"[Sans titre — PMID {pmid}]",
        # `source` est l'abréviation NLM (« N Engl J Med »), celle qu'on affiche.
        journal=(entry.get("source") or entry.get("fulljournalname") or "").strip()
        or None,
        issn=normalize_issn(entry.get("issn")),
        eissn=normalize_issn(entry.get("essn")),
        pub_date=parse_pub_date(entry),
    )


def parse_esearch(payload: dict[str, Any]) -> ESearchResult:
    """Extrait le résultat d'ESearch, avertissements de PubMed compris."""
    result = payload.get("esearchresult", {})
    warninglist = result.get("warninglist", {}) or {}
    errorlist = result.get("errorlist", {}) or {}

    warnings: list[str] = []
    for label, key in (
        ("expression entre guillemets introuvable", "quotedphrasesnotfound"),
        ("terme ignoré", "phrasesignored"),
        ("message", "outputmessages"),
    ):
        for item in warninglist.get(key, []) or []:
            warnings.append(f"{label} : {item}")

    errors: list[str] = []
    for key in ("phrasesnotfound", "fieldsnotfound"):
        for item in errorlist.get(key, []) or []:
            errors.append(f"introuvable : {item}")
    if isinstance(result.get("error"), str):
        errors.append(result["error"])

    return ESearchResult(
        pmids=[str(p) for p in result.get("idlist", [])],
        count=int(result.get("count", 0) or 0),
        translation=(result.get("querytranslation") or "").strip(),
        warnings=warnings,
        errors=errors,
    )


def parse_nlmcatalog_xml(xml: str) -> list[JournalMatch]:
    """Extrait titre, abréviation NLM et ISSN des fiches du catalogue.

    `MedlineTA` est l'abréviation officielle attendue par `[ta]` : c'est elle
    qui traduit « New England Journal of Medicine » en « N Engl J Med ».
    """
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return []

    matches: list[JournalMatch] = []
    for record in root.iter("NLMCatalogRecord"):
        title_node = record.find("./TitleMain/Title")
        title = "".join(title_node.itertext()).strip() if title_node is not None else ""
        if not title:
            continue
        abbreviation = record.findtext("MedlineTA", default="").strip() or None
        issns = [
            normalized
            for node in record.iter("ISSN")
            if (normalized := normalize_issn(node.text))
        ]
        matches.append(
            JournalMatch(title=title.rstrip("."), abbreviation=abbreviation, issns=issns)
        )
    return matches


class PubMedClient:
    """Client asynchrone des E-utilities, à utiliser en gestionnaire de contexte."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": f"{settings.ncbi_tool}/0.1"}
        )
        self._owns_client = client is None
        self._limiter = RateLimiter(settings.ncbi_requests_per_second)

    async def __aenter__(self) -> "PubMedClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _common_params(self) -> dict[str, str]:
        """Étiquette API : `tool` et `email` accompagnent chaque appel.

        Les identifiants restent facultatifs pour permettre un essai sans
        configuration ; le débit tombe alors à 3 req/s.
        """
        params = {"tool": settings.ncbi_tool}
        if settings.ncbi_email:
            params["email"] = settings.ncbi_email
        if settings.ncbi_api_key:
            params["api_key"] = settings.ncbi_api_key
        return params

    async def _get(self, endpoint: str, params: dict[str, Any]) -> httpx.Response:
        await self._limiter.acquire()
        response = await self._client.get(
            f"{BASE_URL}/{endpoint}", params={**self._common_params(), **params}
        )
        response.raise_for_status()
        return response

    async def esearch(
        self,
        term: str,
        *,
        reldate_days: int | None = None,
        retmax: int | None = None,
    ) -> ESearchResult:
        """Exécute la chaîne PubMed telle quelle sur une fenêtre de récence."""
        params: dict[str, Any] = {
            "db": "pubmed",
            "term": term,
            "retmax": retmax if retmax is not None else settings.esearch_retmax,
            "retmode": "json",
            # ESearch n'accepte pas « date » : le schéma de tri récent est
            # « pub_date » (il renvoie sinon un avertissement et trie par
            # pertinence).
            "sort": "pub_date",
        }
        window = reldate_days if reldate_days is not None else settings.refresh_reldate_days
        if window:
            params["reldate"] = window
            params["datetype"] = "edat"  # date d'entrée dans PubMed
        response = await self._get("esearch.fcgi", params)
        return parse_esearch(response.json())

    async def esummary(self, pmids: list[str]) -> list[ArticleSummary]:
        """Métadonnées légères, par lots. Ne récupère aucun résumé."""
        summaries: list[ArticleSummary] = []
        for start in range(0, len(pmids), ESUMMARY_BATCH_SIZE):
            batch = pmids[start : start + ESUMMARY_BATCH_SIZE]
            response = await self._get(
                "esummary.fcgi",
                {"db": "pubmed", "id": ",".join(batch), "retmode": "json"},
            )
            result = response.json().get("result", {})
            for uid in result.get("uids", []):
                parsed = parse_esummary_entry(result.get(uid, {}))
                if parsed is not None:
                    summaries.append(parsed)
        return summaries

    async def resolve_journal(self, name: str, limit: int = 6) -> list[JournalMatch]:
        """Cherche une revue dans le catalogue NLM et renvoie son abréviation.

        Le vocabulaire vient de la NLM, jamais d'une liste maison.
        """
        name = name.strip()
        if not name:
            return []
        search = await self._get(
            "esearch.fcgi",
            {
                "db": "nlmcatalog",
                # `currentlyindexed` écarte les fiches obsolètes ou non indexées.
                "term": f'"{name}"[Title] AND currentlyindexed[All]',
                "retmax": limit,
                "retmode": "json",
            },
        )
        ids = [str(uid) for uid in search.json().get("esearchresult", {}).get("idlist", [])]
        if not ids:
            return []

        # Le JSON d'ESummary laisse `medabbr` vide pour ce dépôt : seul le XML
        # d'EFetch expose MedlineTA.
        fetch = await self._get(
            "efetch.fcgi",
            {"db": "nlmcatalog", "id": ",".join(ids), "retmode": "xml"},
        )
        return parse_nlmcatalog_xml(fetch.text)
