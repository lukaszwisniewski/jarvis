"""Shared logging helpers for tests."""

import contextlib
import logging
from collections.abc import Iterator

from app.logging_config import CorrelationIdFilter

# Loggers whose settings configure_logging() changes.
_TOUCHED_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "httpx",
    "httpcore",
    "httpx2",
    "httpcore2",
)


class RecordCollector(logging.Handler):
    """Keeps LogRecord objects so tests can inspect their attributes."""

    def __init__(self, *, with_correlation_filter: bool = False) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        if with_correlation_filter:
            self.addFilter(CorrelationIdFilter())

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextlib.contextmanager
def restored_logging() -> Iterator[None]:
    """Restore the root logger and the loggers configure_logging() changes."""
    root = logging.getLogger()
    saved_root = (root.handlers[:], root.level)
    saved = {
        name: (
            logging.getLogger(name).handlers[:],
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
        )
        for name in _TOUCHED_LOGGERS
    }
    try:
        yield
    finally:
        root.handlers, root.level = saved_root
        for name, (handlers, level, propagate) in saved.items():
            logger = logging.getLogger(name)
            logger.handlers, logger.level, logger.propagate = handlers, level, propagate
