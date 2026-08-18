"""Point d'entrée « double-clic » : démarre le serveur et ouvre le navigateur.

Appelé par `lancer.cmd`, ou à la main via `uv run python -m app.run`.
Le mode développement (rechargement à chaud) reste `uvicorn app.main:app --reload`.
"""

from __future__ import annotations

import socket
import threading
import time
import webbrowser

import uvicorn

from app.config import settings

HOST = "127.0.0.1"
DEFAULT_PORT = 8000
STARTUP_TIMEOUT_SECONDS = 30.0


def pick_port(host: str, preferred: int) -> int:
    """Le port souhaité s'il est libre, sinon un port attribué par le système.

    Évite l'échec au démarrage quand une session précédente n'a pas été fermée.
    """
    for candidate in (preferred, 0):
        with socket.socket() as probe:
            try:
                probe.bind((host, candidate))
            except OSError:
                continue
            return probe.getsockname()[1]
    raise OSError("Aucun port disponible sur l'interface locale")


def _contact_problems() -> list[str]:
    """Champs d'étiquette API vides ou restés à leur valeur d'exemple.

    Le NCBI et OpenAlex identifient l'appelant par ces champs : une adresse
    `example.com` vaut une absence de contact.
    """
    problems = [f"{name} est vide dans .env" for name in settings.missing_contact_fields()]
    placeholders = {
        "NCBI_EMAIL": settings.ncbi_email,
        "OPENALEX_MAILTO": settings.openalex_mailto,
    }
    problems += [
        f"{name} est resté sur l'adresse d'exemple ({value})"
        for name, value in placeholders.items()
        if value.endswith("example.com")
    ]
    return problems


def _open_when_ready(server: uvicorn.Server, url: str) -> None:
    """Ouvre le navigateur une fois uvicorn en écoute, pas avant."""
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while not server.started:
        if time.monotonic() > deadline:
            return
        time.sleep(0.2)
    webbrowser.open(url)


def main() -> None:
    port = pick_port(HOST, DEFAULT_PORT)
    url = f"http://{HOST}:{port}/"

    for field in _contact_problems():
        print(f"Attention : {field}")
    if _contact_problems():
        print("Les appels sortants doivent porter un contact réel — corrige .env.\n")

    print(f"Veille PubMed démarre sur {url}")
    print("Ferme cette fenêtre ou fais Ctrl+C pour arrêter le serveur.\n")

    server = uvicorn.Server(
        uvicorn.Config("app.main:app", host=HOST, port=port, log_level="info")
    )
    threading.Thread(target=_open_when_ready, args=(server, url), daemon=True).start()
    server.run()


if __name__ == "__main__":
    main()
