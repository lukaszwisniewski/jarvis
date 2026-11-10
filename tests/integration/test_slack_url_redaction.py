"""Slack response_url never reaches a log, through real failing flows.

A real Celery worker runs process_jarvis_query; the Slack POST goes through
a real httpx.Client with a MockTransport. Flows: a timeout then success
(retry), three 503 responses (retries exhausted) and a 404 (client error).
Each run uses the handler that configure_logging() installs, at INFO and
DEBUG, with the JSON and the text formatter.
"""

import io
import json
import logging
import uuid

import httpx
import pytest

import app.workers.tasks as tasks
from app.logging_config import SLACK_URL_REDACTED, configure_logging
from tests.integration.conftest import RecordCollector, task_finished, wait_until

TOKEN = "xoxSECRETTOKEN42"


def record_texts(record: logging.LogRecord) -> list[str]:
    """Every place a record can carry text into an output."""
    exception = record.exc_text or (
        logging.Formatter().formatException(record.exc_info) if record.exc_info else ""
    )
    return [
        record.getMessage(),
        str(record.args),
        exception,
        record.stack_info or "",
        str(getattr(record, "data", "")),
    ]


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
@pytest.mark.parametrize("log_format", ["json", "text"])
def test_slack_url_never_logged(in_memory_celery, fake_slack, level, log_format):
    # Use the real handler from configure_logging(), writing to a buffer.
    configure_logging(level, log_format)
    root = logging.getLogger()
    output = io.StringIO()
    root.handlers[0].setStream(output)
    collector = RecordCollector()
    root.addHandler(collector)  # runs after the real handler and its filters

    flows = {
        "retry": [httpx.ReadTimeout("slack timed out"), 200],
        "exhausted": [503, 503, 503],
        "client_error": [404],
    }
    task_ids = []
    try:
        for name, outcomes in flows.items():
            url = fake_slack.script(
                f"https://hooks.slack.com/commands/T0/B0/{TOKEN}-{name}", *outcomes
            )
            task_id = str(uuid.uuid4())
            tasks.process_jarvis_query.apply_async(
                kwargs={
                    "query": "q",
                    "response_url": url,
                    "user_id": "U1",
                    "scope": None,
                },
                task_id=task_id,
            )
            task_ids.append(task_id)
        for task_id in task_ids:
            wait_until(lambda tid=task_id: task_finished(collector.records, tid))
    finally:
        root.removeHandler(collector)

    # The flows really happened: 2 + 3 + 1 POSTs, and two tasks failed.
    assert len(fake_slack.posts) == 6
    failures = [r for r in collector.records if "raised" in r.getMessage()]
    assert len(failures) == 2
    assert all(r.exc_info for r in failures)  # tracebacks were logged

    text = output.getvalue()
    assert TOKEN not in text
    assert "hooks.slack.com" not in text
    if log_format == "json":
        lines = [json.loads(line) for line in text.splitlines() if line.strip()]
        assert any("exception" in line for line in lines)
        for line in lines:
            assert TOKEN not in json.dumps(line)

    for record in collector.records:
        for value in record_texts(record):
            assert TOKEN not in value, record.getMessage()

    # Sensitivity: the URL was present (Celery puts the kwargs in record.data
    # and the 404/503 errors in the exception text) and was redacted.
    assert any(
        SLACK_URL_REDACTED in str(getattr(r, "data", "")) for r in collector.records
    )
    assert any(SLACK_URL_REDACTED in record_texts(r)[2] for r in failures)
