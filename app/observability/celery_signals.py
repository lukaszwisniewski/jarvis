"""Celery signal handlers for worker logging and the correlation ID.

Imported by app/workers/celery_app.py, so they are connected wherever the
Celery app is loaded. setup_logging only fires in a worker.
"""

from contextvars import Token

from celery import signals

from app.config import settings
from app.logging_config import configure_logging
from app.observability.context import correlation_id
from app.observability.worker_metrics import start_worker_metrics_server

# task_id -> token, so task_postrun can restore the previous value.
_tokens: dict[str, Token] = {}


@signals.setup_logging.connect
def setup_worker_logging(loglevel=None, **kwargs) -> None:
    # Having any receiver for this signal makes Celery skip its own logging
    # setup, so Celery's lines use our JSON handler too. Child processes
    # inherit this setup through fork (Celery does not redo it).
    configure_logging(loglevel or settings.log_level, settings.log_format)


@signals.worker_init.connect
def start_metrics_endpoint(**kwargs) -> None:
    # Runs once in the worker parent, before the pool forks children. It only
    # starts the HTTP server; the metrics directory is cleared before Celery
    # starts (see app/observability/worker_metrics.py), never here.
    start_worker_metrics_server(settings.worker_metrics_port)


@signals.task_prerun.connect
def set_correlation_id(task_id=None, **kwargs) -> None:
    # The API used the correlation ID as the task ID; retries keep it.
    if task_id:
        _tokens[task_id] = correlation_id.set(task_id)


@signals.task_postrun.connect
def reset_correlation_id(task_id=None, **kwargs) -> None:
    token = _tokens.pop(task_id, None)
    if token is not None:
        correlation_id.reset(token)
