import json
import logging
import sys
from unittest.mock import AsyncMock, patch

import pytest

from app.logging_config import CorrelationIdFilter, JsonFormatter, configure_logging
from app.observability.context import client_request_id, correlation_id


class ListHandler(logging.Handler):
    """Collects records after our filter has run, like the real handler."""

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


def make_record(msg="hello", args=None, **extra) -> logging.LogRecord:
    record = logging.LogRecord("app.test", logging.INFO, __file__, 1, msg, args, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_filter_uses_context_var() -> None:
    token = correlation_id.set("cid-1")
    try:
        record = make_record()
        CorrelationIdFilter().filter(record)
    finally:
        correlation_id.reset(token)

    assert record.correlation_id == "cid-1"


def test_filter_prefers_celery_task_id_from_record_data() -> None:
    # Celery passes extra={"data": {"id": ...}} on its own task log lines.
    record = make_record("Task %(name)s[%(id)s] received", data={"id": "task-9"})

    CorrelationIdFilter().filter(record)

    assert record.correlation_id == "task-9"


def test_filter_without_any_id_sets_none() -> None:
    record = make_record()
    CorrelationIdFilter().filter(record)

    assert record.correlation_id is None
    assert record.client_request_id is None


def test_json_formatter_fields() -> None:
    token = client_request_id.set("client-1")
    try:
        record = make_record("value=%s", ("x",), data={"id": "task-1"})
        CorrelationIdFilter().filter(record)
    finally:
        client_request_id.reset(token)

    entry = json.loads(JsonFormatter().format(record))

    assert entry["message"] == "value=x"
    assert entry["level"] == "INFO"
    assert entry["logger"] == "app.test"
    assert entry["correlation_id"] == "task-1"
    assert entry["client_request_id"] == "client-1"
    assert entry["timestamp"].endswith("+00:00")


def test_json_formatter_includes_exception() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = make_record()
        record.exc_info = sys.exc_info()
    CorrelationIdFilter().filter(record)

    entry = json.loads(JsonFormatter().format(record))

    assert "ValueError: boom" in entry["exception"]


def test_configure_logging_routes_uvicorn_loggers_through_root() -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        configure_logging("INFO", "json")
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            assert logging.getLogger(name).handlers == []
            assert logging.getLogger(name).propagate is True
    finally:
        root.handlers, root.level = saved_handlers, saved_level


@patch("app.api.webhooks.slack.process_jarvis_query")
@patch("app.api.webhooks.slack.verify_slack_request", new_callable=AsyncMock)
def test_api_uses_one_id_for_header_task_and_logs(
    mock_verify, mock_task, client, captured
) -> None:
    response = client.post(
        "/slack/commands",
        data={
            "text": "how does login work",
            "response_url": "https://hooks.slack.com/test",
            "user_id": "U123",
        },
        headers={"X-Request-ID": "client-abc"},
    )

    cid = response.headers["X-Correlation-ID"]
    assert mock_task.apply_async.call_args.kwargs["task_id"] == cid
    enqueued = [
        r for r in captured if r.getMessage().startswith("slash command accepted")
    ]
    assert len(enqueued) == 1
    assert enqueued[0].correlation_id == cid
    # The client's ID is kept separately and never becomes the task ID.
    assert enqueued[0].client_request_id == "client-abc"
    assert cid != "client-abc"


def test_each_request_gets_a_new_correlation_id(client) -> None:
    first = client.get("/health").headers["X-Correlation-ID"]
    second = client.get("/health").headers["X-Correlation-ID"]

    assert first != second
    assert correlation_id.get() is None  # reset after the request
