"""Client Ollama : résumé français d'un abstract, produit par un LLM local.

Le service tourne sur la même machine que l'application : aucune donnée ne sort
du serveur et il n'y a pas de clé d'API. Ce module ne fait que reformuler un
abstract déjà en base — il n'introduit aucune nouvelle source bibliographique.
"""

from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass

import httpx

from app.config import settings

# Une seule génération à la fois. Sur un GPU modeste, deux inférences
# simultanées font déborder la VRAM et tout part sur le CPU.
_GENERATION_SLOT = asyncio.Semaphore(1)

# Sonde de disponibilité : elle doit échouer vite, l'interface l'attend.
_PROBE_TIMEOUT = 2.0

SYSTEM_PROMPT = """Tu es un assistant de veille bibliographique médicale.

Ta tâche : résumer en FRANÇAIS le résumé (abstract) d'un article scientifique,
pour un médecin qui trie sa veille.

Règles impératives :
- Réponds uniquement en français.
- Un seul paragraphe, de 3 à 4 phrases, 120 mots au maximum. Pas de titre,
  pas de puces, pas de préambule du type « Voici le résumé ».
- Conserve tels quels les chiffres clés : effectifs, doses, pourcentages,
  intervalles de confiance, valeurs de p.
- N'ajoute aucune information absente du texte fourni. Si le texte ne dit pas
  quelque chose, ne le dis pas.
- Ne traduis pas les noms de molécules, d'échelles ni d'essais cliniques.
- Si le texte ne contient pas de résultats (éditorial, lettre), dis-le en une
  phrase plutôt que d'inventer."""


@dataclass(frozen=True, slots=True)
class Summary:
    """Résumé produit, et de quoi juger le coût de sa production."""

    text: str
    model: str
    eval_count: int
    eval_seconds: float
    load_seconds: float
    # Vrai si le modèle a heurté le plafond `num_predict` : le texte est alors
    # coupé en pleine phrase, et le plafond est à relever.
    truncated: bool

    @property
    def tokens_per_second(self) -> float:
        return self.eval_count / self.eval_seconds if self.eval_seconds else 0.0


def build_user_prompt(title: str, abstract: str) -> str:
    """Message utilisateur. Fonction pure : testable sans réseau."""
    return f"Titre : {title}\n\nRésumé original :\n{abstract}"


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def clean_output(raw: str) -> str:
    """Nettoie la sortie du modèle.

    `think: false` suffit normalement, mais un modèle de raisonnement peut
    ignorer le drapeau et livrer son monologue : on le retire par sécurité.
    """
    return _THINK_BLOCK.sub("", raw).strip()


class OllamaClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=float(settings.ollama_timeout_seconds)
        )
        self._owns_client = client is None

    async def __aenter__(self) -> OllamaClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def is_available(self) -> bool:
        """Vrai si Ollama répond. Sert à masquer la fonction plutôt qu'à échouer."""
        try:
            response = await self._client.get(
                f"{settings.ollama_base_url}/api/tags", timeout=_PROBE_TIMEOUT
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return False
        return True

    async def summarize(
        self, title: str, abstract: str, model: str | None = None
    ) -> Summary:
        """Résume l'abstract en français. Laisse remonter les erreurs HTTP.

        `model` sert au banc d'essai, qui compare plusieurs modèles sans
        toucher au `.env`. L'application n'y touche pas.
        """
        chosen = model or settings.ollama_model
        payload = {
            "model": chosen,
            "stream": False,
            # Les modèles « thinking » (qwen3.5…) brûleraient des centaines de
            # tokens de raisonnement avant de répondre.
            "think": False,
            "keep_alive": settings.ollama_keep_alive,
            "options": {
                "temperature": 0.2,
                "num_ctx": settings.ollama_num_ctx,
                "num_predict": 700,
            },
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(title, abstract)},
            ],
        }
        async with _GENERATION_SLOT:
            response = await self._client.post(
                f"{settings.ollama_base_url}/api/chat", json=payload
            )
            response.raise_for_status()
            body = response.json()

        return Summary(
            text=clean_output((body.get("message") or {}).get("content") or ""),
            model=body.get("model") or chosen,
            eval_count=int(body.get("eval_count") or 0),
            eval_seconds=(body.get("eval_duration") or 0) / 1e9,
            load_seconds=(body.get("load_duration") or 0) / 1e9,
            truncated=body.get("done_reason") == "length",
        )


# --- Banc d'essai en ligne de commande ------------------------------------

# Abstract réel (STEP-HFpEF, NEJM), pour éprouver le prompt sans dépendre du
# contenu de la base : dense en chiffres, c'est le cas qui fait fauter un
# petit modèle.
DEMO_TITLE = "Semaglutide in Patients with Heart Failure and Obesity"
DEMO_ABSTRACT = (
    "BACKGROUND: The efficacy and safety of semaglutide in patients with heart "
    "failure with preserved ejection fraction (HFpEF) and obesity are unknown. "
    "METHODS: We randomly assigned 529 patients who had HFpEF and a body-mass "
    "index of 30 or more, without diabetes, to receive once-weekly semaglutide "
    "(2.4 mg) or placebo for 52 weeks. The dual primary end points were the "
    "change from baseline in the Kansas City Cardiomyopathy Questionnaire "
    "clinical summary score (KCCQ-CSS) and the percentage change in body "
    "weight. RESULTS: The mean change in the KCCQ-CSS was 16.6 points with "
    "semaglutide and 8.7 points with placebo (estimated difference, 7.8 "
    "points; 95% CI, 4.8 to 10.9; P<0.001), and the mean percentage change in "
    "body weight was -13.3% with semaglutide and -2.6% with placebo "
    "(estimated difference, -10.7 percentage points; 95% CI, -11.9 to -9.4; "
    "P<0.001). Serious adverse events were reported in 35 participants "
    "(13.3%) in the semaglutide group and 71 (26.7%) in the placebo group. "
    "CONCLUSIONS: Among patients with HFpEF and obesity, semaglutide led to "
    "larger reductions in symptoms and physical limitations and greater "
    "weight loss than placebo."
)


def _load_article(pmid: str) -> tuple[str, str] | None:
    """Titre et abstract d'un article en base, ou None avec un message."""
    from app.db import get_conn
    from app.repo import get_abstract, get_article_summary

    with get_conn(settings.db_path) as conn:
        article = get_article_summary(conn, pmid)
        abstract = get_abstract(conn, pmid)

    if article is None:
        print(f"PMID {pmid} absent de la base. Rafraîchis une requête d'abord.")
        return None
    if not abstract:
        print(f"PMID {pmid} n'a pas de résumé en cache. Ouvre-le dans l'interface.")
        return None
    return article.title, abstract


async def _bench(models: list[str], pmid: str | None) -> int:
    """Compare des modèles sur le prompt réel de l'application."""
    if pmid is None:
        title, abstract = DEMO_TITLE, DEMO_ABSTRACT
    else:
        loaded = _load_article(pmid)
        if loaded is None:
            return 1
        title, abstract = loaded

    print(f"Article : {title}\n")
    async with OllamaClient() as llm:
        if not await llm.is_available():
            print(f"Ollama ne répond pas sur {settings.ollama_base_url}.")
            return 1
        for model in models:
            barre = "=" * 70
            print(f"{barre}\n{model}\n{barre}")
            try:
                summary = await llm.summarize(title, abstract, model=model)
            except httpx.HTTPError as exc:
                print(f"ÉCHEC : {exc}\n")
                continue
            print(summary.text)
            print(
                f"\n[ {summary.eval_count} tokens en {summary.eval_seconds:.1f} s "
                f"= {summary.tokens_per_second:.1f} tok/s | "
                f"chargement {summary.load_seconds:.1f} s ]"
            )
            if summary.truncated:
                print("ATTENTION : sortie coupée par num_predict.")
            print()

    print("Vérifie la répartition GPU/CPU avec : ollama ps")
    return 0


def main() -> int:
    """Usage : python -m app.llm [--model m1,m2] [PMID]"""
    args = sys.argv[1:]
    models = [settings.ollama_model]
    if args and args[0] == "--model":
        if len(args) < 2:
            print("--model attend une liste de modèles séparés par des virgules.")
            return 2
        models = [m.strip() for m in args[1].split(",") if m.strip()]
        args = args[2:]
    if len(args) > 1:
        print("Usage : python -m app.llm [--model m1,m2] [PMID]")
        return 2
    return asyncio.run(_bench(models, args[0] if args else None))


if __name__ == "__main__":
    raise SystemExit(main())
