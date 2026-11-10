import logging
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.logging_config import CorrelationIdFilter
from app.main import app
from app.observability.metrics import API_REGISTRY


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self.addFilter(CorrelationIdFilter())

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


SLACK_FORM = {
    "text": "unique-question-text",
    "response_url": "https://hooks.slack.com/test",
    "user_id": "U-UNIQUE-USER",
}


def sample(name: str, **labels) -> float:
    return API_REGISTRY.get_sample_value(name, labels) or 0.0


def test_metrics_endpoint_serves_only_api_metrics(client) -> None:
    client.get("/health")

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "jarvis_api_request_duration_seconds" in response.text
    # Worker metrics exist in this process (the API imports the worker code)
    # but must not be exposed here.
    assert "jarvis_task_duration_seconds" not in response.text
    assert "jarvis_source_request_duration_seconds" not in response.text


@patch("app.api.webhooks.slack.process_jarvis_query")
@patch("app.api.webhooks.slack.verify_slack_request", new_callable=AsyncMock)
def test_request_latency_counted_by_status_class(
    mock_verify, mock_task, client
) -> None:
    before = sample("jarvis_api_request_duration_seconds_count", outcome="2xx")
    before_404 = sample("jarvis_api_request_duration_seconds_count", outcome="4xx")

    client.post("/slack/commands", data=SLACK_FORM)  # counted as 2xx
    client.get("/does-not-exist")  # counted as 4xx
    client.get("/health")  # health probes are not counted
    client.get("/metrics")  # scrapes are not counted

    assert (
        sample("jarvis_api_request_duration_seconds_count", outcome="2xx") == before + 1
    )
    assert (
        sample("jarvis_api_request_duration_seconds_count", outcome="4xx")
        == before_404 + 1
    )


@patch("app.api.webhooks.slack.process_jarvis_query")
@patch("app.api.webhooks.slack.verify_slack_request", new_callable=AsyncMock)
def test_enqueue_success_is_counted(mock_verify, mock_task, client) -> None:
    before = sample("jarvis_api_tasks_enqueued_total", outcome="success")

    client.post("/slack/commands", data=SLACK_FORM)

    assert sample("jarvis_api_tasks_enqueued_total", outcome="success") == before + 1
    # Labels never contain user input.
    text = client.get("/metrics").text
    assert "unique-question-text" not in text
    assert "U-UNIQUE-USER" not in text


@patch("app.api.webhooks.slack.process_jarvis_query")
@patch("app.api.webhooks.slack.verify_slack_request", new_callable=AsyncMock)
def test_enqueue_failure_is_counted_and_still_returns_500(
    mock_verify, mock_task
) -> None:
    mock_task.apply_async.side_effect = ConnectionError("redis down")
    before = sample("jarvis_api_tasks_enqueued_total", outcome="error")
    before_5xx = sample("jarvis_api_request_duration_seconds_count", outcome="5xx")

    response = TestClient(app, raise_server_exceptions=False).post(
        "/slack/commands", data=SLACK_FORM
    )

    assert response.status_code == 500
    assert sample("jarvis_api_tasks_enqueued_total", outcome="error") == before + 1
    assert (
        sample("jarvis_api_request_duration_seconds_count", outcome="5xx")
        == before_5xx + 1
    )


@patch("app.api.webhooks.slack.process_jarvis_query")
@patch("app.api.webhooks.slack.verify_slack_request", new_callable=AsyncMock)
def test_enqueue_failure_is_logged_with_the_correlation_id(
    mock_verify, mock_task
) -> None:
    mock_task.apply_async.side_effect = ConnectionError("redis down")
    handler = ListHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        TestClient(app, raise_server_exceptions=False).post(
            "/slack/commands", data=SLACK_FORM
        )
    finally:
        root.removeHandler(handler)

    task_id = mock_task.apply_async.call_args.kwargs["task_id"]
    failures = [
        r for r in handler.records if r.getMessage() == "failed to enqueue task"
    ]
    assert len(failures) == 1
    assert failures[0].levelno == logging.ERROR
    assert failures[0].correlation_id == task_id
    assert failures[0].exc_info is not None
