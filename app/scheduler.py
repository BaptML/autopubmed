"""Job de fond : rafraîchissement périodique des requêtes.

Intégré au process uvicorn et désactivé par défaut (`ENABLE_SCHEDULER`). C'est
lui qui nourrit le cache entre deux passages de l'utilisateur : l'interface se
contente ensuite de lire la base, et le compteur de non-lus se remplit tout
seul.
"""

from __future__ import annotations

import logging
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.db import get_conn
from app.refresh import refresh_all

logger = logging.getLogger("app.scheduler")

JOB_ID = "refresh-all"


async def run_refresh(db_path: Path | None = None) -> None:
    """Un passage complet sur toutes les requêtes, avec sa propre connexion.

    Le mode WAL permet ce passage pendant que le serveur sert des pages. Une
    panne réseau ne doit jamais tuer le job : elle est journalisée et le
    prochain passage réessaiera.
    """
    try:
        with get_conn(db_path) as conn:
            reports = await refresh_all(conn)
    except Exception:  # le job doit survivre à tout, y compris à nos bugs
        logger.exception("Rafraîchissement de fond interrompu")
        return

    new_links = sum(report.new_links for report in reports)
    logger.info(
        "Rafraîchissement de fond : %d requête(s), %d nouvel(s) article(s)",
        len(reports),
        new_links,
    )
    for report in reports:
        for error in report.errors:
            logger.warning("« %s » : %s", report.query_name, error)


def create_scheduler() -> AsyncIOScheduler | None:
    """Prépare le planificateur, ou None si le job est désactivé."""
    if not settings.enable_scheduler:
        logger.info("Job de fond désactivé (ENABLE_SCHEDULER)")
        return None

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_refresh,
        IntervalTrigger(minutes=settings.scheduler_interval_minutes),
        id=JOB_ID,
        # Un seul passage à la fois : un rafraîchissement long ne doit pas se
        # superposer au suivant, sinon deux passages tapent PubMed ensemble.
        max_instances=1,
        # Machine en veille ou serveur arrêté : les passages manqués sont
        # fusionnés en un seul, jamais rejoués en rafale.
        coalesce=True,
        misfire_grace_time=600,
    )
    logger.info(
        "Job de fond actif : toutes les %d minutes",
        settings.scheduler_interval_minutes,
    )
    return scheduler
