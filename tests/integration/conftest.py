"""Shared fixtures: a real Celery worker on the in-memory broker, and a fake
Slack endpoint served by httpx.MockTransport. Nothing leaves the process."""

import logging
import time
from collections.abc import Callable
from unittest.mock import patch

import httpx
import pytest
from celery.contrib.testing.worker import start_worker

import app.workers.tasks as tasks
from app.config import settings
from app.workers.celery_app import celery_app


class RecordCollector(logging.Handler):
    """Keeps the LogRecord objects so tests can inspect their attributes."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def in_memory_celery():
    """Start a real worker (solo pool, in a thread) on the in-memory broker.

    Starting the worker fires setup_logging, so the root handler is the one
    our configure_logging() installs.
    """
    saved = {
        "broker_url": celery_app.conf.broker_url,
        "result_backend": celery_app.conf.result_backend,
    }
    celery_app.conf.update(broker_url="memory://", result_backend="cache+memory://")
    celery_app._pool = None  # drop any connection pool built for Redis
    with start_worker(celery_app, perform_ping_check=False, loglevel="INFO"):
        yield
    celery_app.conf.update(**saved)
    celery_app._pool = None


class FakeSlack:
    """Answers POSTs to Slack response URLs from a per-URL script.

    Each script item is a status code or an exception to raise.
    """

    def __init__(self) -> None:
        self.scripts: dict[str, list] = {}
        self.posts: list[str] = []

    def script(self, url: str, *outcomes) -> str:
        self.scripts[url] = list(outcomes)
        return url

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.posts.append(url)
        outcome = self.scripts[url].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, request=request)


@pytest.fixture
def fake_slack():
    """Route the task's Slack POST to FakeSlack and stub retrieval and Claude.

    Only tasks._slack_client is patched, not httpx.Client globally, so the
    real httpx.Client (with its logging and errors) is used.
    """
    slack = FakeSlack()
    transport = httpx.MockTransport(slack.handle)
    with (
        patch.object(settings, "agent_mode", False),
        patch.object(tasks, "SLACK_RETRY_COUNTDOWN_SECONDS", 0),
        patch.object(tasks, "_build_context", return_value=""),
        patch.object(tasks, "ask_llm", return_value="answer"),
        patch.object(tasks, "_slack_client", lambda: httpx.Client(transport=transport)),
    ):
        yield slack


def wait_until(condition: Callable[[], bool], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the worker")
        time.sleep(0.05)


def task_finished(records: list[logging.LogRecord], task_id: str) -> bool:
    """True once Celery logged success or failure for this task."""
    return any(
        isinstance(getattr(r, "data", None), dict)
        and r.data.get("id") == task_id
        and ("succeeded" in r.getMessage() or "raised" in r.getMessage())
        for r in records
    )
