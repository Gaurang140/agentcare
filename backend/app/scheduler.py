"""Two APScheduler background jobs: sending due reminders (every minute) and
escalating workflow runs stuck in "running" (every 10 minutes). Both job
bodies are plain functions elsewhere (`app.tools.followup_tools.
send_due_reminders`, `app.services.workflow_service.
escalate_stalled_workflows`) - this module only wires them to a schedule,
each with its own fresh db session.

Guarded against starting twice under pytest: `start_scheduler()` is a no-op
whenever the TESTING env var is set (tests set it in conftest.py before
importing app.main, so every TestClient's lifespan sees it).
"""

from __future__ import annotations

import os

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.db import session as db_session_module
from app.logging_setup import get_logger
from app.services.workflow_service import escalate_stalled_workflows
from app.tools.followup_tools import send_due_reminders

logger = get_logger(__name__)

_REMINDER_INTERVAL_SECONDS = 60
_STALLED_CHECK_INTERVAL_SECONDS = 600

_scheduler: BackgroundScheduler | None = None


def _run_reminder_job(*, raise_errors: bool = False) -> None:
    db = db_session_module.SessionLocal()
    try:
        result = send_due_reminders(db)
        logger.info("reminder_job_ran", **result)
    except Exception:  # noqa: BLE001 - a scheduler job must never crash the process
        logger.error("reminder_job_failed", exc_info=True)
        if raise_errors:
            raise
    finally:
        db.close()


def _run_stalled_job(*, raise_errors: bool = False) -> None:
    db = db_session_module.SessionLocal()
    try:
        result = escalate_stalled_workflows(db)
        logger.info("stalled_job_ran", **result)
    except Exception:  # noqa: BLE001 - a scheduler job must never crash the process
        logger.error("stalled_job_failed", exc_info=True)
        if raise_errors:
            raise
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler | None:
    """Start the two jobs, unless TESTING is set or it's already running.
    Returns the scheduler (or None under TESTING) so the lifespan can hold
    onto it for a clean shutdown."""
    global _scheduler
    if os.environ.get("TESTING") or not settings.scheduler_enabled:
        return None
    if _scheduler is not None:
        return _scheduler

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _run_reminder_job, "interval", seconds=_REMINDER_INTERVAL_SECONDS, id="send_due_reminders"
    )
    scheduler.add_job(
        _run_stalled_job,
        "interval",
        seconds=_STALLED_CHECK_INTERVAL_SECONDS,
        id="escalate_stalled_workflows",
    )
    scheduler.start()
    _scheduler = scheduler
    return scheduler


def stop_scheduler() -> None:
    """Shut down the scheduler if running. Safe to call when it never
    started (TESTING, or start_scheduler was never called)."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
