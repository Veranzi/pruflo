"""Pruflo Cloud Run worker (Docs/backend_plan.md, Step 7).

- POST /pubsub/push: Pub/Sub push subscription delivers one message.
  2xx acks it; 503 nacks it so Pub/Sub retries with backoff and, after the
  subscription's maximum deliveries, moves it to the dead-letter topic.
- POST /tasks/dispatch-outbox: Cloud Scheduler (every minute) publishes any
  outbox events the API couldn't publish straight away.
- POST /tasks/retry-webhooks: Cloud Scheduler (every minute) re-sends failed
  webhook deliveries on the contract's schedule.
- POST /tasks/sync-connectors: Cloud Scheduler (every 15 minutes) imports new
  files from Odoo, Google Drive and SharePoint.
- POST /tasks/reconcile: Cloud Scheduler (every 15 minutes) runs the rules
  for any case whose files are done but that wasn't reconciled.
- POST /tasks/retention: Cloud Scheduler (daily) deletes reports and evidence
  past their retention periods (both off by default).

Both require a Google-signed OIDC token for one of the allowed service
accounts. The service must not be reachable without it.
"""

import base64
import binascii
import json
import logging
from typing import Any

from fastapi import Depends, FastAPI, Header, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import database_ready, get_db
from app.errors import forbidden, install_error_handlers, unauthorized
from app.logging import configure_logging
from app.middleware import install_request_middleware
from app.worker.jobs import RetryLater
from app.worker.queue import Queue, dispatch_outbox, get_queue, handle_event

logger = logging.getLogger("pruflo.worker")


def verify_invoker(authorization: str | None = Header(default=None)) -> None:
    """Only Pub/Sub and Cloud Scheduler may call the worker."""
    settings = get_settings()
    if settings.env in ("local", "test"):
        return
    if not settings.worker_push_audience or not settings.worker_invoker_service_accounts:
        raise forbidden("The worker isn't configured to accept calls.")  # fail closed
    if not authorization or not authorization.startswith("Bearer "):
        raise unauthorized()
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    try:
        claims: dict[str, Any] = id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
            authorization.removeprefix("Bearer "),
            google_requests.Request(),
            audience=settings.worker_push_audience,
        )
    except ValueError as exc:
        raise unauthorized("Invalid token.") from exc
    if not claims.get("email_verified") or (
        claims.get("email") not in settings.worker_invoker_service_accounts
    ):
        raise forbidden()


class PushMessage(BaseModel):
    data: str = ""
    attributes: dict[str, str] = {}
    messageId: str = ""  # noqa: N815 (Pub/Sub's field name)


class PushEnvelope(BaseModel):
    message: PushMessage
    subscription: str = ""


def create_worker_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="Pruflo worker", version="0.1.0", docs_url=None, redoc_url=None)
    install_error_handlers(app)
    install_request_middleware(app)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "release": get_settings().release}

    @app.get("/readyz", response_model=None)
    def readyz(db: Session = Depends(get_db)) -> dict[str, str] | JSONResponse:
        return database_ready(db)

    @app.post("/pubsub/push", dependencies=[Depends(verify_invoker)])
    def pubsub_push(
        envelope: PushEnvelope,
        db: Session = Depends(get_db),
        queue: Queue = Depends(get_queue),
    ) -> Response:
        message = envelope.message
        try:
            payload = json.loads(base64.b64decode(message.data)) if message.data else {}
        except (binascii.Error, json.JSONDecodeError):
            # It will never parse, so retrying can't help: ack and log it.
            logger.error("dropping malformed message %s", message.messageId)
            return Response(status_code=204)
        try:
            outcome = handle_event(
                db, queue.storage, queue.ocr, message.attributes.get("type", ""), payload
            )
        except RetryLater:
            return Response(status_code=503)
        except KeyError:
            logger.error("dropping message %s with missing fields", message.messageId)
            return Response(status_code=204)
        logger.info("message %s: %s", message.messageId, outcome)
        # Jobs write follow-up events (e.g. evidence.processed); send them on now
        # rather than waiting for the next scheduled sweep.
        dispatch_outbox(db, queue.publisher)
        return Response(status_code=204)

    @app.post("/tasks/dispatch-outbox", dependencies=[Depends(verify_invoker)])
    def dispatch(
        db: Session = Depends(get_db), queue: Queue = Depends(get_queue)
    ) -> dict[str, int]:
        return {"published": len(dispatch_outbox(db, queue.publisher))}

    @app.post("/tasks/weekly-summary", dependencies=[Depends(verify_invoker)])
    def weekly_summary(
        db: Session = Depends(get_db), queue: Queue = Depends(get_queue)
    ) -> dict[str, int]:
        """Cloud Scheduler, Mondays: in-app summaries; notifications past 90 days removed."""
        from app.services import notifications

        sent = notifications.weekly_summary(db)
        removed = notifications.purge_old(db)
        dispatch_outbox(db, queue.publisher)
        return {"sent": sent, "removed": removed}

    @app.post("/tasks/retry-webhooks", dependencies=[Depends(verify_invoker)])
    def retry_webhooks(db: Session = Depends(get_db)) -> dict[str, int]:
        """Cloud Scheduler, every minute: re-send failed webhook deliveries that are due."""
        from app.services import webhooks

        return {"sent": webhooks.retry_due(db)}

    @app.post("/tasks/sync-connectors", dependencies=[Depends(verify_invoker)])
    def sync_connectors(
        db: Session = Depends(get_db), queue: Queue = Depends(get_queue)
    ) -> dict[str, int]:
        """Cloud Scheduler, every 15 minutes: import new files from polled sources."""
        from app.connectors.sync import sync_due

        totals = sync_due(db, queue.storage)
        dispatch_outbox(db, queue.publisher)
        return totals

    @app.post("/tasks/reconcile", dependencies=[Depends(verify_invoker)])
    def reconcile(
        db: Session = Depends(get_db), queue: Queue = Depends(get_queue)
    ) -> dict[str, int]:
        from app.services.reconcile import reconcile_waiting

        done = reconcile_waiting(db)
        dispatch_outbox(db, queue.publisher)
        return {"reconciled": done}

    @app.post("/tasks/retention", dependencies=[Depends(verify_invoker)])
    def retention(
        db: Session = Depends(get_db), queue: Queue = Depends(get_queue)
    ) -> dict[str, int]:
        from app.services import retention as retention_service

        return retention_service.run(db, queue.storage)

    return app


app = create_worker_app()
