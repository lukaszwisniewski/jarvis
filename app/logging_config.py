"""Logging setup shared by the API and the Celery worker.

Every log line gets a `correlation_id` field. JSON is the default format; set
LOG_FORMAT=text for the old human-readable style (with the same field).
"""

import json
import logging
import re
from datetime import datetime, timezone

from app.observability.context import client_request_id, correlation_id

TEXT_FORMAT = (
    "%(asctime)s | %(levelname)s | %(name)s | "
    "correlation_id=%(correlation_id)s | %(message)s"
)

# Uvicorn installs its own handlers on these loggers; we route them through
# the root handler instead so their lines use the same format.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")

# httpx logs every request URL at INFO ("HTTP Request: POST https://..."),
# which would write Slack's response_url to the log on every answer. These
# loggers are kept at WARNING even when LOG_LEVEL=DEBUG.
_HTTP_CLIENT_LOGGERS = ("httpx", "httpcore", "httpx2", "httpcore2")

# Slack response URLs (hooks.slack.com, hooks-global.slack.com,
# hooks.eu.slack.com) let anyone who has them post into the user's Slack,
# so they are treated as credentials and never written to a log.
_SLACK_URL_RE = re.compile(r"https?://hooks[\w.-]*\.slack\.com/[^\s'\"<>)\]]*", re.I)
SLACK_URL_REDACTED = "[REDACTED_SLACK_URL]"


def redact_slack_urls(text: str) -> str:
    return _SLACK_URL_RE.sub(SLACK_URL_REDACTED, text)


def _redact_value(value):
    if isinstance(value, str):
        return redact_slack_urls(value)
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_value(item) for item in value)
    return value


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


def _safe_repr(value) -> str:
    try:
        return repr(value)
    except Exception:
        return "<unrepresentable>"


class SlackUrlRedactingFilter(logging.Filter):
    """Safety net: remove Slack response URLs from everything a record carries.

    Celery's own task lines include the task kwargs (with response_url) in
    record.args and record.data, and exception text can contain the URL.
    The message is formatted once here and replaced by its redacted text;
    the exception text is formatted and redacted into record.exc_text,
    which both formatters use.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Handler filters run outside logging's own error handling, so an
        # exception here would reach the code that logged. Never raise.
        try:
            self._redact(record)
        except Exception:
            # Last resort: drop everything that could carry a URL.
            record.msg = "[log record dropped: redaction failed]"
            record.args = None
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            if hasattr(record, "data"):
                record.data = None
        return True

    def _redact(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:
            # A malformed call such as logger.info("%s %s", "x"). Keep what
            # was logged, as repr, so it is still useful, but redacted.
            message = (
                "[unformattable log message] "
                f"msg={_safe_repr(record.msg)} args={_safe_repr(record.args)}"
            )
        record.msg = redact_slack_urls(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_slack_urls(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_slack_urls(record.stack_info)
        data = getattr(record, "data", None)
        if data is not None:
            record.data = _redact_value(data)


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
        if record.exc_info or record.exc_text:
            # exc_text is the redacted text set by SlackUrlRedactingFilter.
            entry["exception"] = record.exc_text or self.formatException(
                record.exc_info
            )
        return json.dumps(entry, default=str)


def resolve_log_level(configured: str | None, fallback: str | int) -> str | int:
    """LOG_LEVEL wins when it is set; otherwise use the process's own default."""
    return configured.upper() if configured else fallback


def configure_logging(level: str | int = "INFO", log_format: str = "json") -> None:
    """Replace the root handler with one stderr handler in the chosen format."""
    handler = logging.StreamHandler()
    # Order matters: the correlation filter reads record.data before the
    # redaction filter replaces it with a redacted copy.
    handler.addFilter(CorrelationIdFilter())
    handler.addFilter(SlackUrlRedactingFilter())
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

    for name in _HTTP_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
