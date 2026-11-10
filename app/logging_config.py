"""Logging setup shared by the API and the Celery worker.

Every log line gets a `correlation_id` field. JSON is the default format; set
LOG_FORMAT=text for the old human-readable style (with the same field).
"""

import json
import logging
from datetime import datetime, timezone

from app.observability.context import client_request_id, correlation_id

TEXT_FORMAT = (
    "%(asctime)s | %(levelname)s | %(name)s | "
    "correlation_id=%(correlation_id)s | %(message)s"
)

# Uvicorn installs its own handlers on these loggers; we route them through
# the root handler instead so their lines use the same format.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class CorrelationIdFilter(logging.Filter):
    """Add correlation_id (and client_request_id) to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Celery's own task lines ("received", "retry", "succeeded", "raised")
        # carry the task context as record.data. "received" is logged by the
        # worker parent before task_prerun runs, so the ContextVar is not set
        # yet; the task ID in record.data is the correlation ID.
        data = getattr(record, "data", None)
        if isinstance(data, dict) and data.get("id"):
            record.correlation_id = str(data["id"])
        else:
            record.correlation_id = correlation_id.get()
        record.client_request_id = client_request_id.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with a fixed set of fields."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "process": record.processName,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", None),
        }
        client_id = getattr(record, "client_request_id", None)
        if client_id:
            entry["client_request_id"] = client_id
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str | int = "INFO", log_format: str = "json") -> None:
    """Replace the root handler with one stderr handler in the chosen format."""
    handler = logging.StreamHandler()
    handler.addFilter(CorrelationIdFilter())
    if log_format == "text":
        handler.setFormatter(logging.Formatter(TEXT_FORMAT))
    else:
        handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
