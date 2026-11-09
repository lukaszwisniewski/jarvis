import hashlib
import hmac
import time

from fastapi import Request

from app.config import settings
from app.exceptions import SlackVerificationError


async def verify_slack_request(request: Request) -> None:
    """Verify the request genuinely came from Slack using HMAC signature.

    Slack signs every request with your signing secret. Without this check,
    anyone who knows your endpoint URL could send fake slash commands.

    Raises:
        SlackVerificationError: If the signature is missing, expired, or invalid.
    """
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not timestamp or not signature:
        raise SlackVerificationError("Missing Slack signature headers")

    try:
        timestamp_int = int(timestamp)
    except ValueError as exc:
        raise SlackVerificationError("Invalid Slack timestamp") from exc
    if timestamp_int < 0:
        raise SlackVerificationError("Invalid Slack timestamp")

    # Reject requests older than 5 minutes — prevents replay attacks
    if abs(time.time() - timestamp_int) > 300:
        raise SlackVerificationError("Request timestamp too old")

    signing_secret = settings.slack_signing_secret
    if not signing_secret:
        raise SlackVerificationError("Slack signing secret is not configured")

    body = await request.body()
    base_string = b"v0:" + timestamp.encode("ascii") + b":" + body
    expected = (
        "v0="
        + hmac.new(
            signing_secret.encode(),
            base_string,
            hashlib.sha256,
        ).hexdigest()
    )

    if not hmac.compare_digest(expected, signature):
        raise SlackVerificationError("Invalid Slack signature")
