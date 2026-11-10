"""End-to-end correlation test: API -> Celery (in-memory broker) -> worker.

A real Celery worker runs process_jarvis_query in a thread, using Celery's
in-memory transport, so no Redis and no network is needed. The Slack POST
times out once to force a retry. The test then checks the correlation_id
attribute that our logging filter puts on every LogRecord.
"""

import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from celery.contrib.testing.worker import start_worker

import app.workers.tasks as tasks
from app.logging_config import CorrelationIdFilter
from app.workers.celery_app import celery_app


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []
        self.addFilter(CorrelationIdFilter())

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def captured():
    handler = ListHandler()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    yield handler.records
    root.removeHandler(handler)
    root.setLevel(old_level)


@pytest.fixture
def in_memory_celery():
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


def wait_until(condition, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the worker")
        time.sleep(0.05)


@patch("app.api.webhooks.slack.verify_slack_request", new_callable=AsyncMock)
def test_same_correlation_id_in_api_worker_celery_and_retry(
    mock_verify, client, in_memory_celery, captured
) -> None:
    # Fixture order matters: starting the worker fires setup_logging, which
    # replaces the root handlers, so the capture handler is added afterwards.
    app_logger = logging.getLogger("app.test.worker")

    def fake_build_context(query, scope):
        app_logger.info("building context inside the task")
        return ""

    ok = MagicMock(status_code=200)
    ok.raise_for_status.return_value = None
    slack_client = MagicMock()
    slack_client.__enter__.return_value.post.side_effect = [
        httpx.TimeoutException("slack timed out"),  # first run -> retry
        ok,  # second run -> success
    ]

    with (
        patch.object(tasks, "SLACK_RETRY_COUNTDOWN_SECONDS", 0),
        patch.object(tasks, "_build_context", side_effect=fake_build_context),
        patch.object(tasks, "ask_llm", return_value="answer"),
        patch.object(tasks.httpx, "Client", return_value=slack_client),
    ):
        response = client.post(
            "/slack/commands",
            data={
                "text": "how does login work",
                "response_url": "https://hooks.slack.com/test",
                "user_id": "U123",
            },
        )
        cid = response.headers["X-Correlation-ID"]
        wait_until(
            lambda: any(
                "succeeded" in r.getMessage() and r.correlation_id == cid
                for r in captured
            )
        )

    def records_where(predicate):
        return [r for r in captured if predicate(r)]

    def celery_line(word):
        # Celery's own task lines carry the task context as record.data.
        return records_where(
            lambda r: (
                r.name.startswith("celery")
                and isinstance(getattr(r, "data", None), dict)
                and word in r.getMessage()
            )
        )

    api_lines = records_where(
        lambda r: r.getMessage() == "slash command accepted; task enqueued"
    )
    received = celery_line("received")
    retried = celery_line("retry")
    succeeded = celery_line("succeeded")
    app_lines = records_where(lambda r: r.name == "app.test.worker")

    assert len(api_lines) == 1
    assert len(received) == 2  # original delivery and the retry
    assert len(retried) == 1
    assert len(succeeded) == 1
    assert len(app_lines) == 2  # the task body ran twice

    for record in api_lines + received + retried + succeeded + app_lines:
        assert record.correlation_id == cid, record.getMessage()
    # Celery's own records really carry the task ID (not just in the text).
    for record in received + retried + succeeded:
        assert record.data["id"] == cid
