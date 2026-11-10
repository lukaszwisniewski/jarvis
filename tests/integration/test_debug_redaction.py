"""A worker started at DEBUG never writes the Slack response_url anywhere.

Celery decides at pool start whether to log its DEBUG lines, including
"TaskPool: Apply ...", which contains the whole task message with the
kwargs. So the worker itself is started at DEBUG here. A raw handler placed
before ours records what each record carried before redaction, to show the
URL really was there.
"""

import io
import json
import logging
import uuid
from unittest.mock import patch

import httpx
import pytest

import app.workers.tasks as tasks
from app.config import settings
from tests.integration.conftest import task_finished, wait_until
from tests.log_capture import RecordCollector

TOKEN = "xoxSECRETTOKEN42"


class RawSnapshot(logging.Handler):
    """Runs before our handler and keeps the unredacted text of each record."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.items: list[tuple[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        text = " ".join([record.getMessage(), str(getattr(record, "data", ""))])
        self.items.append((record.name, text))


def texts(record: logging.LogRecord) -> list[str]:
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


@pytest.fixture(params=["json", "text"])
def worker_log_format(request):
    # Patched before the worker starts, because setup_logging reads it then.
    with (
        patch.object(settings, "log_format", request.param),
        patch.object(settings, "log_level", None),
    ):
        yield request.param


@pytest.mark.parametrize("in_memory_celery", ["DEBUG"], indirect=True)
def test_debug_worker_never_logs_the_slack_url(
    worker_log_format, in_memory_celery, fake_slack
) -> None:
    root = logging.getLogger()
    assert root.level == logging.DEBUG

    # Every logger reaches the root handlers: none stops propagation or has
    # a real handler of its own, so capturing at the root captures all.
    bypassing = [
        name
        for name, logger in logging.root.manager.loggerDict.items()
        if isinstance(logger, logging.Logger)
        and (
            not logger.propagate
            or any(not isinstance(h, logging.NullHandler) for h in logger.handlers)
        )
    ]
    assert bypassing == []

    output = io.StringIO()
    installed = root.handlers[0]  # from our setup_logging receiver
    installed.setStream(output)
    raw = RawSnapshot()
    collector = RecordCollector()
    root.handlers.insert(0, raw)  # sees records before our filters
    root.addHandler(collector)  # sees them after

    flows = {
        "retry": [httpx.ReadTimeout("slack timed out"), 200],
        "client_error": [404],
    }
    urls: dict[str, str] = {}
    task_ids: dict[str, str] = {}
    try:
        for name, outcomes in flows.items():
            # A non-default port: httpx drops :443, so the URL it sends would
            # no longer match the scripted one and FakeSlack would fail.
            url = fake_slack.script(
                f"https://user:pw@hooks.slack.com:8443/commands/T0/B0/{TOKEN}-{name}",
                *outcomes,
            )
            urls[name] = url
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
            task_ids[name] = task_id
        for task_id in task_ids.values():
            wait_until(lambda tid=task_id: task_finished(collector.records, tid))
    finally:
        root.removeHandler(raw)
        root.removeHandler(collector)

    # The flows really ran, each as scripted.
    assert fake_slack.posts.count(urls["retry"]) == 2  # timeout, then 200
    assert fake_slack.posts.count(urls["client_error"]) == 1  # 404, no retry
    assert len(fake_slack.posts) == 3

    def final_lines(task_id: str) -> list[logging.LogRecord]:
        return [
            r
            for r in collector.records
            if isinstance(getattr(r, "data", None), dict)
            and r.data.get("id") == task_id
            and ("succeeded" in r.getMessage() or "raised" in r.getMessage())
        ]

    retry_final = final_lines(task_ids["retry"])
    assert len(retry_final) == 1
    assert "succeeded" in retry_final[0].getMessage()

    error_final = final_lines(task_ids["client_error"])
    assert len(error_final) == 1
    assert "raised" in error_final[0].getMessage()
    assert error_final[0].exc_info[0] is httpx.HTTPStatusError

    # No other task failed, and nothing unexpected (such as a KeyError from
    # an unscripted URL) was raised or logged anywhere.
    failed = [r for r in collector.records if "raised" in r.getMessage()]
    assert failed == error_final
    for record in collector.records:
        assert "KeyError" not in " ".join(texts(record)), record.getMessage()[:80]

    # Sensitivity: before redaction the URL was in Celery's DEBUG pool line.
    apply_lines = [text for name, text in raw.items if "TaskPool: Apply" in text]
    assert apply_lines, "the DEBUG 'TaskPool: Apply' line was not logged"
    assert any(TOKEN in text for text in apply_lines)
    carriers = sorted({name for name, text in raw.items if TOKEN in text})
    assert "celery.pool" in carriers

    # After our handler: the token is nowhere.
    written = output.getvalue()
    assert "TaskPool: Apply" in written
    assert TOKEN not in written
    assert "hooks.slack.com" not in written
    if worker_log_format == "json":
        for line in written.splitlines():
            if line.startswith("{"):
                assert TOKEN not in json.dumps(json.loads(line))
    for record in collector.records:
        for value in texts(record):
            assert TOKEN not in value, (record.name, record.getMessage()[:80])
