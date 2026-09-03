"""Configuration lue depuis `.env`, exposée en un objet immuable unique."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Racine du projet : le dossier parent de `app/`.
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} doit être un entier, reçu : {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    """Paramètres d'exécution. Instancié une fois au chargement du module."""

    ncbi_api_key: str | None
    ncbi_tool: str
    ncbi_email: str
    openalex_mailto: str
    db_path: Path
    refresh_reldate_days: int
    esearch_retmax: int
    enable_scheduler: bool
    scheduler_interval_minutes: int
    # Durée au-delà de laquelle rouvrir une requête compte pour une nouvelle
    # session, et décale le repère du badge « nouveau ». En deçà, recharger la
    # liste n'efface pas les badges qu'on est en train de regarder.
    visit_session_gap_minutes: int

    @property
    def ncbi_requests_per_second(self) -> int:
        """Plafond imposé par le NCBI : 10 req/s avec une clé API, 3 sans."""
        return 10 if self.ncbi_api_key else 3

    def missing_contact_fields(self) -> list[str]:
        """Champs d'étiquette API vides. Les appels sortants doivent les avoir."""
        missing: list[str] = []
        if not self.ncbi_email:
            missing.append("NCBI_EMAIL")
        if not self.openalex_mailto:
            missing.append("OPENALEX_MAILTO")
        return missing


def _load() -> Settings:
    db_path = Path(os.getenv("DB_PATH", "data/veille.db"))
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path

    return Settings(
        ncbi_api_key=os.getenv("NCBI_API_KEY", "").strip() or None,
        ncbi_tool=os.getenv("NCBI_TOOL", "pubmed-veille").strip(),
        ncbi_email=os.getenv("NCBI_EMAIL", "").strip(),
        openalex_mailto=os.getenv("OPENALEX_MAILTO", "").strip(),
        db_path=db_path,
        refresh_reldate_days=_env_int("REFRESH_RELDATE_DAYS", 90),
        esearch_retmax=_env_int("ESEARCH_RETMAX", 200),
        enable_scheduler=_env_bool("ENABLE_SCHEDULER", False),
        scheduler_interval_minutes=_env_int("SCHEDULER_INTERVAL_MINUTES", 360),
        visit_session_gap_minutes=_env_int("VISIT_SESSION_GAP_MINUTES", 30),
    )


settings: Settings = _load()
