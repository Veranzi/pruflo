"""Scheduled work for hosts without a separate worker or Cloud Scheduler.

``POST /v1/internal/tick`` runs everything the worker's scheduled routes do,
in one call: process files still waiting (including retries), reconcile
cases missed after a crash, re-send failed webhooks, import from document
sources, apply retention, and send the Monday summary. Every step is safe to
repeat. A GitHub Actions cron calls it every 30 minutes with
``X-Pruflo-Tick-Token``; on free hosts that sleep, the call also wakes it.
"""

import hmac
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_db
from app.errors import not_found, unauthorized
from app.worker.queue import Queue, dispatch_outbox, get_queue

logger = logging.getLogger("pruflo.tick")
router = APIRouter(prefix="/internal", include_in_schema=False)
SUMMARY_ZONE = ZoneInfo("Asia/Singapore")


def _check(token: str | None) -> None:
    expected = get_settings().tick_token
    if not expected:
        raise not_found()  # switched off unless a token is configured
    if not token or not hmac.compare_digest(token.encode(), expected.encode()):
        raise unauthorized()


@router.post("/tick")
def tick(
    token: str | None = Header(default=None, alias="X-Pruflo-Tick-Token"),
    db: Session = Depends(get_db),
    queue: Queue = Depends(get_queue),
) -> dict[str, object]:
    _check(token)
    from app.connectors.sync import sync_due
    from app.services import notifications, retention
    from app.worker.__main__ import drain

    done: dict[str, object] = {}
    steps: dict[str, Callable[[], object]] = {
        "processing": lambda: drain(db),  # waiting files, retries, reconcile, webhook retries
        "connectors": lambda: sync_due(db, queue.storage),
        "retention": lambda: retention.run(db, queue.storage),
        "notifications_removed": lambda: notifications.purge_old(db),
    }
    local = datetime.now(UTC).astimezone(SUMMARY_ZONE)
    if local.weekday() == 0 and local.hour >= 7:
        steps["weekly_summaries"] = lambda: notifications.weekly_summary(db)
    for name, step in steps.items():
        try:
            done[name] = step()
        except Exception:
            # One failing step mustn't stop the others; the next tick retries.
            db.rollback()
            logger.exception("tick step %s failed", name)
            done[name] = "failed"
    dispatch_outbox(db, queue.publisher)
    queue.kick()  # anything the steps queued runs now, in this process
    return done
