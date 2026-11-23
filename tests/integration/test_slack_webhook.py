import asyncio
import threading
import time
from unittest.mock import ANY, AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app


@patch("app.api.webhooks.slack.process_jarvis_query")
@patch("app.api.webhooks.slack.verify_slack_request", new_callable=AsyncMock)
@patch("app.api.webhooks.slack.settings")
def test_slash_command_acks_immediately(
    mock_settings, mock_verify, mock_task, client: TestClient
):
    """Route acks with ephemeral message and enqueues the Celery task."""
    mock_settings.slack_allowed_user_ids = "U123"
    response = client.post(
        "/slack/commands",
        data={
            "text": "hello",
            "response_url": "https://hooks.slack.com/test",
            "user_id": "U123",
            "user_name": "lukasz",
        },
    )

    assert response.status_code == 200
    assert response.json()["response_type"] == "ephemeral"
    mock_task.apply_async.assert_called_once_with(
        kwargs={
            "query": "hello",
            "response_url": "https://hooks.slack.com/test",
            "user_id": "U123",
            "scope": None,
        },
        task_id=ANY,
    )


@patch("app.api.webhooks.slack.process_jarvis_query")
@patch("app.api.webhooks.slack.verify_slack_request", new_callable=AsyncMock)
@patch("app.api.webhooks.slack.settings")
def test_slash_command_scope_prefix_cf(
    mock_settings, mock_verify, mock_task, client: TestClient
):
    """Route parses explicit cf prefix and passes normalized scope."""
    mock_settings.slack_allowed_user_ids = ""
    response = client.post(
        "/slack/commands",
        data={
            "text": "cf how are thumbnails generated",
            "response_url": "https://hooks.slack.com/test",
            "user_id": "U123",
        },
    )

    assert response.status_code == 200
    mock_task.apply_async.assert_called_once_with(
        kwargs={
            "query": "how are thumbnails generated",
            "response_url": "https://hooks.slack.com/test",
            "user_id": "U123",
            "scope": "coldfusion",
        },
        task_id=ANY,
    )


@patch("app.api.webhooks.slack.process_jarvis_query")
@patch("app.api.webhooks.slack.verify_slack_request", new_callable=AsyncMock)
@patch("app.api.webhooks.slack.settings")
def test_slash_command_rejects_unauthorised_user(
    mock_settings, mock_verify, mock_task, client: TestClient
):
    """Route returns ephemeral denial and does NOT enqueue task for unlisted user."""
    mock_settings.slack_allowed_user_ids = "U123"
    response = client.post(
        "/slack/commands",
        data={
            "text": "hello",
            "response_url": "https://hooks.slack.com/test",
            "user_id": "U999",
            "user_name": "intruder",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["response_type"] == "ephemeral"
    assert (
        "can't assist" in body["text"].lower() or "restricted" in body["text"].lower()
    )
    mock_task.apply_async.assert_not_called()


@patch("app.api.webhooks.slack.process_jarvis_query")
@patch("app.api.webhooks.slack.verify_slack_request", new_callable=AsyncMock)
@patch("app.api.webhooks.slack.settings")
def test_slash_command_rejects_missing_fields(
    mock_settings, mock_verify, mock_task, client: TestClient
):
    """Route returns 400 and does NOT enqueue task when required fields are absent."""
    mock_settings.slack_allowed_user_ids = ""
    response = client.post(
        "/slack/commands",
        data={
            "text": "hello",
            "response_url": "",
            "user_id": "",
            "user_name": "",
        },
    )

    assert response.status_code == 400
    mock_task.apply_async.assert_not_called()


@patch("app.api.webhooks.slack.process_jarvis_query")
@patch("app.api.webhooks.slack.verify_slack_request", new_callable=AsyncMock)
@patch("app.api.webhooks.slack.settings")
@pytest.mark.parametrize(
    "bad_url",
    [
        "http://hooks.slack.com/test",  # non-HTTPS scheme
        "https://evil.example.com/callback",  # non-Slack host
        "https://hooks.slack.com.evil.com/x",  # subdomain spoofing
    ],
)
def test_slash_command_rejects_invalid_response_url(
    mock_settings, mock_verify, mock_task, bad_url, client: TestClient
):
    """Route returns 400 and does not enqueue a task for a bad response_url.

    A bad URL is non-HTTPS or not on a Slack domain.
    """
    mock_settings.slack_allowed_user_ids = ""
    response = client.post(
        "/slack/commands",
        data={
            "text": "hello",
            "response_url": bad_url,
            "user_id": "U123",
        },
    )

    assert response.status_code == 400
    mock_task.apply_async.assert_not_called()


@patch("app.api.webhooks.slack.process_jarvis_query")
@patch(
    "app.api.webhooks.slack.verify_slack_request",
    new_callable=AsyncMock,
    side_effect=__import__(
        "app.exceptions", fromlist=["SlackVerificationError"]
    ).SlackVerificationError(),
)
def test_slash_command_rejects_invalid_signature(
    mock_verify, mock_task, client: TestClient
):
    """Route returns 403 and does NOT enqueue the task when verification fails."""
    response = client.post(
        "/slack/commands",
        data={
            "text": "hello",
            "response_url": "https://hooks.slack.com/test",
            "user_id": "U123",
            "user_name": "lukasz",
        },
        headers={
            "X-Slack-Request-Timestamp": str(int(time.time())),
            "X-Slack-Signature": "v0=invalidsignature",
        },
    )

    assert response.status_code == 403
    mock_task.apply_async.assert_not_called()


@pytest.mark.asyncio
@patch("app.api.webhooks.slack.process_jarvis_query")
@patch("app.api.webhooks.slack.verify_slack_request", new_callable=AsyncMock)
@patch("app.api.webhooks.slack.settings")
async def test_slow_enqueue_does_not_block_other_requests(
    mock_settings, mock_verify, mock_task
):
    """While the broker call is in progress, other requests are still served.

    The fake apply_async blocks until /health has finished. That only works
    if the enqueue runs off the event loop; otherwise /health cannot run and
    the wait times out.
    """
    mock_settings.slack_allowed_user_ids = "U123"
    started = threading.Event()
    release = threading.Event()
    events: list[str] = []
    released: list[bool] = []

    def slow_enqueue(**kwargs) -> None:
        events.append("enqueue start")
        started.set()
        released.append(release.wait(2))  # False means it timed out
        events.append("enqueue end")

    mock_task.apply_async.side_effect = slow_enqueue

    async def health_during_enqueue(client: httpx.AsyncClient) -> httpx.Response:
        try:
            # Wait in a thread: started.wait() here would block the loop itself.
            assert await asyncio.to_thread(started.wait, 2), "enqueue never started"
            response = await client.get("/health")
            events.append("health done")
            return response
        finally:
            release.set()  # never leave the enqueue thread waiting

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        command, health = await asyncio.gather(
            ac.post(
                "/slack/commands",
                data={
                    "text": "hello",
                    "response_url": "https://hooks.slack.com/test",
                    "user_id": "U123",
                    "user_name": "lukasz",
                },
            ),
            health_during_enqueue(ac),
        )

    assert events == ["enqueue start", "health done", "enqueue end"]
    assert released == [True]
    assert command.status_code == 200
    assert health.status_code == 200
