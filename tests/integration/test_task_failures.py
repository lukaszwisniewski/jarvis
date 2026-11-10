"""Slack POST failures in a real Celery worker: retry reasons, retries
running out, a 4xx, the Celery failure line, and the JSON handler that
setup_logging installs."""

import io
import json
import logging
import re
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
    # Exact POSTs per flow, and every scripted response was actually served.
    # (FakeSlack records a POST before looking up its script, so the count
    # alone would also match a KeyError from an unscripted URL; an emptied
    # script proves the intended response was returned.)
    urls = {name: f"https://hooks.slack.com/commands/{name}" for name in flows}
    assert fake_slack.posts.count(urls["network"]) == 2  # ConnectError, then 200
    assert fake_slack.posts.count(urls["exhausted"]) == 3  # 503, 503, 503
    assert fake_slack.posts.count(urls["client_error"]) == 1  # 404, no retry
    assert len(fake_slack.posts) == 6
    for name, url in urls.items():
        remaining = fake_slack.scripts.get(url)
        assert remaining == [], f"{name}: scripted responses not used: {remaining}"

    def celery_lines(task_id: str, pattern: str) -> list[logging.LogRecord]:
        """Celery's own lines for one task, matched by its message format."""
        regex = re.compile(rf"\[{re.escape(task_id)}\] {pattern}")
        return [
            r
            for r in collector.records
            if r.name.startswith("celery") and regex.search(r.getMessage())
        ]

    # Retry lines use Celery's format "Task <name>[<id>] retry: <exc>".
    network_retries = celery_lines(task_ids["network"], "retry: ")
    assert len(network_retries) == 1
    assert "ConnectError" in network_retries[0].getMessage()  # the first attempt
    exhausted_retries = celery_lines(task_ids["exhausted"], "retry: ")
    assert len(exhausted_retries) == 2
    assert all("503" in r.getMessage() for r in exhausted_retries)
    assert celery_lines(task_ids["client_error"], "retry: ") == []

    # Final outcomes.
    assert len(celery_lines(task_ids["network"], "succeeded")) == 1  # retry worked
    assert celery_lines(task_ids["network"], "raised") == []
    expected_failures = {
        "exhausted": "'503 Service Unavailable'",
        "client_error": "'404 Not Found'",
    }
    for name, status in expected_failures.items():
        failed = celery_lines(task_ids[name], "raised")
        assert len(failed) == 1, name
        assert failed[0].levelno == logging.ERROR
        # Celery cannot pickle HTTPStatusError (it needs request/response), so
        # the logged exception object is a plain httpx.HTTPError without
        # .response; exc_info[0] keeps the original type. The status is
        # therefore checked in the error message.
        exc_type, error, _ = failed[0].exc_info
        assert exc_type is httpx.HTTPStatusError, (name, exc_type)
        assert status in str(error), (name, str(error))
        # Celery's failure line carries the correlation ID as an attribute.
        assert failed[0].correlation_id == task_ids[name]
        assert celery_lines(task_ids[name], "succeeded") == []

    # Nothing else failed, and no unexpected exception (such as a KeyError
    # from an unscripted URL) was raised or logged anywhere.
    all_failures = [
        r
        for r in collector.records
        if r.name.startswith("celery") and " raised " in r.getMessage()
    ]
    assert len(all_failures) == 2
    for record in collector.records:
        text = record.getMessage() + (record.exc_text or "")
        assert "KeyError" not in text, record.getMessage()[:80]

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
