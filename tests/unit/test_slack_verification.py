import hashlib
import hmac
import time

import pytest
from fastapi import Request

from app.config import settings
from app.exceptions import SlackVerificationError
from app.slack.client import verify_slack_request


def _make_request(body: str, timestamp: str, signature: str) -> Request:
    """Build a minimal Starlette Request with ASGI receive callable for testing."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/slack/commands",
        "headers": [
            (b"x-slack-request-timestamp", timestamp.encode()),
            (b"x-slack-signature", signature.encode()),
        ],
    }
    encoded = body.encode()

    async def receive():
        return {"type": "http.request", "body": encoded, "more_body": False}

    return Request(scope, receive=receive)


def _sign(body: str, timestamp: str, secret: str) -> str:
    base = f"v0:{timestamp}:{body}"
    return "v0=" + hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_valid_signature_passes():
    body = "text=hello&user_id=U123"
    timestamp = str(int(time.time()))
    sig = _sign(body, timestamp, settings.slack_signing_secret)
    request = _make_request(body, timestamp, sig)
    await verify_slack_request(request)  # should not raise


@pytest.mark.asyncio
async def test_invalid_signature_raises():
    body = "text=hello&user_id=U123"
    timestamp = str(int(time.time()))
    request = _make_request(body, timestamp, "v0=invalidsig")
    with pytest.raises(SlackVerificationError):
        await verify_slack_request(request)


@pytest.mark.asyncio
async def test_expired_timestamp_raises():
    body = "text=hello"
    old_timestamp = str(int(time.time()) - 400)  # 400s ago — beyond 5min window
    sig = _sign(body, old_timestamp, settings.slack_signing_secret)
    request = _make_request(body, old_timestamp, sig)
    with pytest.raises(SlackVerificationError):
        await verify_slack_request(request)


@pytest.mark.asyncio
async def test_missing_headers_raises():
    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}

    async def receive():
        return {"type": "http.request", "body": b"text=hello", "more_body": False}

    request = Request(scope, receive=receive)
    with pytest.raises(SlackVerificationError):
        await verify_slack_request(request)
