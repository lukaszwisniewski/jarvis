"""End-to-end correlation test: API -> Celery (in-memory broker) -> worker.

A real Celery worker runs process_jarvis_query in a thread, using Celery's
in-memory transport, so no Redis and no network is needed. The Slack POST
times out once to force a retry. The test then checks the correlation_id
attribute that our logging filter puts on every LogRecord.
"""

import logging
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from prometheus_client import REGISTRY

import app.workers.tasks as tasks
from app.logging_config import CorrelationIdFilter
from tests.integration.conftest import RecordCollector, wait_until

SLACK_URL = "https://hooks.slack.com/test"


@pytest.fixture
def captured():
    # Added after the worker has started: starting it fires setup_logging,
    # which replaces the root handlers.
    collector = RecordCollector()
    collector.addFilter(CorrelationIdFilter())
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(collector)
    root.setLevel(logging.INFO)
    yield collector.records
    root.removeHandler(collector)
    root.setLevel(old_level)


@patch("app.api.webhooks.slack.verify_slack_request", new_callable=AsyncMock)
def test_same_correlation_id_in_api_worker_celery_and_retry(
    mock_verify, client, in_memory_celery, fake_slack, captured
) -> None:
    app_logger = logging.getLogger("app.test.worker")

    def fake_build_context(query, scope):
        app_logger.info("building context inside the task")
        return ""

    # The real httpx.Client posts through a MockTransport (only
    # tasks._slack_client is patched): a timeout first, then 200.
    fake_slack.script(SLACK_URL, httpx.ReadTimeout("slack timed out"), 200)

    def metric(name, **labels):
        return REGISTRY.get_sample_value(name, labels) or 0.0

    duration = "jarvis_task_duration_seconds_count"
    before = {
        "retries": metric("jarvis_task_retries_total", reason="slack_timeout"),
        "retry_runs": metric(duration, mode="pipeline", outcome="retry"),
        "success_runs": metric(duration, mode="pipeline", outcome="success"),
    }

    with patch.object(tasks, "_build_context", side_effect=fake_build_context):
        response = client.post(
            "/slack/commands",
            data={
                "text": "how does login work",
                "response_url": SLACK_URL,
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

    assert fake_slack.posts == [SLACK_URL, SLACK_URL]

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

    # The retry is visible in metrics: one retry, one retried run, one success.
    retries = metric("jarvis_task_retries_total", reason="slack_timeout")
    assert retries == before["retries"] + 1
    assert (
        metric(duration, mode="pipeline", outcome="retry") == before["retry_runs"] + 1
    )
    assert (
        metric(duration, mode="pipeline", outcome="success")
        == before["success_runs"] + 1
    )
