"""Structured JSON logs that Cloud Logging parses.

Never log document text, signed URLs, tokens or API keys.
"""

import json
import logging
import sys
from typing import Any

from app.context import request_id_var

# Extra fields a log call may attach, e.g. logger.info("...", extra={"case_id": ...}).
_CONTEXT_FIELDS = ("case_id", "evidence_id", "job_id", "finding_id", "status_code", "path", "ms")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
        }
        for name in _CONTEXT_FIELDS:
            if hasattr(record, name):
                entry[name] = getattr(record, name)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


_base_factory = logging.getLogRecordFactory()


def _record_with_request_id(*args: Any, **kwargs: Any) -> logging.LogRecord:
    # Stamp the request ID when the record is created, not when it's formatted,
    # so buffered or threaded handlers still get the right one.
    record = _base_factory(*args, **kwargs)
    record.request_id = request_id_var.get()
    return record


def configure_logging(level: int = logging.INFO) -> None:
    logging.setLogRecordFactory(_record_with_request_id)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # fontTools (used by fpdf2 for PDF reports) logs every subsetting step.
    logging.getLogger("fontTools").setLevel(logging.WARNING)
