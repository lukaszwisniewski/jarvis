"""Slack POST failures in a real Celery worker: retry reasons, retries
running out, a 4xx, the Celery failure line, and the JSON handler that
setup_logging installs."""

import io
import json
import logging
import uuid
from unittest.mock import patch

import httpx
import pytest
from prometheus_client import REGISTRY

import app.workers.tasks as tasks
from app.config import settings
from app.logging_config import JsonFormatter
from tests.integration.conftest import task_finished, wait_until
from tests.log_capture import RecordCollector

DURATION = "jarvis_task_duration_seconds_count"


def metric(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
def json_log_format():
    # Patched before the worker starts, because setup_logging reads it then;
    # a local .env cannot change the format under test.
    with (
        patch.object(settings, "log_format", "json"),
        patch.object(settings, "log_level", None),
    ):
        yield


def test_retry_reasons_exhaustion_and_client_error(
    json_log_format, in_memory_celery, fake_slack
) -> None:
    root = logging.getLogger()
    installed = root.handlers[0]  # installed by our setup_logging receiver
    output = io.StringIO()
    installed.setStream(output)
    collector = RecordCollector()
    root.addHandler(collector)

    before = {
        "network": metric("jarvis_task_retries_total", reason="slack_network"),
        "5xx": metric("jarvis_task_retries_total", reason="slack_5xx"),
        "timeout": metric("jarvis_task_retries_total", reason="slack_timeout"),
        "retry": metric(DURATION, mode="pipeline", outcome="retry"),
        "success": metric(DURATION, mode="pipeline", outcome="success"),
        "failure": metric(DURATION, mode="pipeline", outcome="failure"),
    }
    flows = {
        "network": [httpx.ConnectError("connection reset"), 200],
        "exhausted": [503, 503, 503],
        "client_error": [404],
    }
    task_ids = {}
    try:
        for name, outcomes in flows.items():
            url = fake_slack.script(
                f"https://hooks.slack.com/commands/{name}", *outcomes
            )
            task_ids[name] = str(uuid.uuid4())
            tasks.process_jarvis_query.apply_async(
                kwargs={
                    "query": "q",
                    "response_url": url,
                    "user_id": "U1",
                    "scope": None,
                },
                task_id=task_ids[name],
            )
        for task_id in task_ids.values():
            wait_until(lambda tid=task_id: task_finished(collector.records, tid))
    finally:
        root.removeHandler(collector)

    # Retry reasons: one network retry; two 5xx retries, then no more.
    assert (
        metric("jarvis_task_retries_total", reason="slack_network")
        == before["network"] + 1
    )
    assert metric("jarvis_task_retries_total", reason="slack_5xx") == before["5xx"] + 2
    assert (
        metric("jarvis_task_retries_total", reason="slack_timeout") == before["timeout"]
    )
    # Runs: network (retry, success), exhausted (retry, retry, failure), 404 (failure).
    assert metric(DURATION, mode="pipeline", outcome="retry") == before["retry"] + 3
    assert metric(DURATION, mode="pipeline", outcome="success") == before["success"] + 1
    assert metric(DURATION, mode="pipeline", outcome="failure") == before["failure"] + 2
    # The 404 was posted once (no retry); the 503s three times.
    posts = fake_slack.posts
    assert posts.count("https://hooks.slack.com/commands/client_error") == 1
    assert posts.count("https://hooks.slack.com/commands/exhausted") == 3

    # Celery's failure line carries the correlation ID as an attribute.
    for name in ("exhausted", "client_error"):
        failed = [
            r
            for r in collector.records
            if "raised" in r.getMessage()
            and isinstance(getattr(r, "data", None), dict)
            and r.data.get("id") == task_ids[name]
        ]
        assert len(failed) == 1
        assert failed[0].levelno == logging.ERROR
        assert failed[0].correlation_id == task_ids[name]

    # The handler installed by setup_logging writes one JSON object per line,
    # with the correlation ID on Celery's own lines.
    assert isinstance(installed.formatter, JsonFormatter)
    lines = [json.loads(line) for line in output.getvalue().splitlines() if line]
    for name, task_id in task_ids.items():
        own = [line for line in lines if task_id in line["message"]]
        assert own, name
        assert all(line["correlation_id"] == task_id for line in own)
    failure_lines = [line for line in lines if "raised" in line["message"]]
    assert len(failure_lines) == 2
    assert all("exception" in line for line in failure_lines)
