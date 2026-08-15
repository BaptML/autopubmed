"""Client OpenAlex : citedness d'une revue (équivalent gratuit du facteur d'impact).

Interrogé une seule fois par revue inconnue, puis mis en cache dans
`journal_quality`.
"""

from __future__ import annotations

import httpx

from app.config import settings
from app.pubmed import RateLimiter

BASE_URL = "https://api.openalex.org/sources"

# OpenAlex tolère 10 req/s ; on reste en deçà, d'autant qu'une revue n'est
# interrogée qu'une fois avant mise en cache.
REQUESTS_PER_SECOND = 5


def format_issn(normalized: str) -> str:
    """OpenAlex attend la forme à tiret : « 00284793 » → « 0028-4793 »."""
    return f"{normalized[:4]}-{normalized[4:]}"


class OpenAlexClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None
        self._limiter = RateLimiter(REQUESTS_PER_SECOND)

    async def __aenter__(self) -> "OpenAlexClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_citedness(self, normalized_issn: str) -> float | None:
        """Renvoie `2yr_mean_citedness`, ou None si la revue est inconnue.

        Une revue absente d'OpenAlex n'est pas une erreur : on renvoie None et
        l'appelant décidera (le filtre citedness l'écartera s'il est actif).
        """
        params = {"filter": f"issn:{format_issn(normalized_issn)}", "per-page": "1"}
        if settings.openalex_mailto:
            params["mailto"] = settings.openalex_mailto  # polite pool

        try:
            await self._limiter.acquire()
            response = await self._client.get(BASE_URL, params=params)
            response.raise_for_status()
        except httpx.HTTPError:
            return None

        results = response.json().get("results") or []
        if not results:
            return None
        stats = results[0].get("summary_stats") or {}
        value = stats.get("2yr_mean_citedness")
        return float(value) if value is not None else None
