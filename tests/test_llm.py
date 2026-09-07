"""Tests du client Ollama. Aucun appel réseau : tout passe par MockTransport."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from app.config import settings
from app.llm import OllamaClient, build_user_prompt, clean_output

REPONSE_TYPE = {
    "model": "qwen3.5:4b",
    "message": {"role": "assistant", "content": "Un résumé en français."},
    "eval_count": 120,
    "eval_duration": 6_000_000_000,
    "load_duration": 1_500_000_000,
    "done_reason": "stop",
}


def _client(handler) -> OllamaClient:
    return OllamaClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def _resumer(handler, titre="Titre", abstract="Abstract"):
    async with _client(handler) as llm:
        return await llm.summarize(titre, abstract)


# --- Fonctions pures -----------------------------------------------------


def test_le_prompt_porte_le_titre_et_le_resume() -> None:
    prompt = build_user_prompt("Semaglutide in HFpEF", "BACKGROUND: ...")
    assert "Semaglutide in HFpEF" in prompt
    assert "BACKGROUND: ..." in prompt


def test_le_monologue_de_raisonnement_est_retire() -> None:
    """`think: false` devrait suffire ; on se protège d'un modèle qui l'ignore."""
    brut = "<think>Voyons voir...</think>\n\nLe résumé utile."
    assert clean_output(brut) == "Le résumé utile."


def test_la_sortie_est_deballee_des_espaces() -> None:
    assert clean_output("  texte  \n") == "texte"


# --- Appel à Ollama ------------------------------------------------------


def test_le_resume_et_les_metriques_sont_lus() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=REPONSE_TYPE)

    summary = asyncio.run(_resumer(handler))

    assert summary.text == "Un résumé en français."
    assert summary.model == "qwen3.5:4b"
    assert summary.eval_count == 120
    assert summary.tokens_per_second == pytest.approx(20.0)


def test_la_charge_desactive_le_raisonnement_et_le_flux(monkeypatch) -> None:
    """Sans `think: false`, un modèle de raisonnement double le temps d'attente.

    Le mode est fixé explicitement : sinon le test lirait le `.env` du poste et
    changerait de comportement d'une machine à l'autre.
    """
    charge = _charge_envoyee(monkeypatch, ollama_use_model_prompt=False)

    assert charge["think"] is False
    assert charge["stream"] is False
    assert charge["model"] == settings.ollama_model
    assert charge["options"]["num_ctx"] == settings.ollama_num_ctx
    assert charge["messages"][0]["role"] == "system"
    assert "français" in charge["messages"][0]["content"]


def _charge_envoyee(monkeypatch, **reglages) -> dict:
    """Corps JSON réellement transmis à Ollama, sous les réglages donnés."""
    # Settings est gelé : on substitue l'objet entier, pas un de ses champs.
    monkeypatch.setattr("app.llm.settings", replace(settings, **reglages))
    vues: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        vues.append(json.loads(request.content))
        return httpx.Response(200, json=REPONSE_TYPE)

    asyncio.run(_resumer(handler))
    return vues[0]


def test_le_prompt_du_modele_remplace_le_notre(monkeypatch) -> None:
    """Un message `system` écraserait le SYSTEM du Modelfile : on n'en met pas."""
    charge = _charge_envoyee(monkeypatch, ollama_use_model_prompt=True)

    roles = [m["role"] for m in charge["messages"]]
    assert roles == ["user"]
    # La température appartient alors au Modelfile, pas à nous.
    assert "temperature" not in charge["options"]


def test_les_garde_fous_restent_dans_les_deux_modes(monkeypatch) -> None:
    """num_predict et num_ctx tiennent à la machine, pas au prompt."""
    for mode in (True, False):
        charge = _charge_envoyee(monkeypatch, ollama_use_model_prompt=mode)
        assert charge["options"]["num_predict"] == 700
        assert charge["options"]["num_ctx"] == settings.ollama_num_ctx
        assert charge["think"] is False


def test_par_defaut_nous_envoyons_notre_prompt(monkeypatch) -> None:
    charge = _charge_envoyee(monkeypatch, ollama_use_model_prompt=False)

    assert [m["role"] for m in charge["messages"]] == ["system", "user"]
    assert charge["options"]["temperature"] == 0.2


def test_l_erreur_http_remonte_a_l_appelant() -> None:
    """Comme PubMedClient : c'est la route qui décide du message affiché."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connexion refusée")

    with pytest.raises(httpx.HTTPError):
        asyncio.run(_resumer(handler))


def test_une_reponse_en_erreur_remonte_aussi() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "model not found"})

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_resumer(handler))


# --- Sonde de disponibilité ----------------------------------------------


def test_ollama_est_disponible_quand_api_tags_repond() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": []})

    async def verifier() -> bool:
        async with _client(handler) as llm:
            return await llm.is_available()

    assert asyncio.run(verifier()) is True


def test_ollama_absent_ne_leve_pas() -> None:
    """L'interface doit pouvoir masquer la fonction, pas planter."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("service arrêté")

    async def verifier() -> bool:
        async with _client(handler) as llm:
            return await llm.is_available()

    assert asyncio.run(verifier()) is False
