import logging
from urllib.parse import urlparse

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    status,
)

from app.config import settings
from app.exceptions import SlackVerificationError
from app.observability.context import correlation_id, new_correlation_id
from app.observability.metrics import TASKS_ENQUEUED
from app.slack.client import verify_slack_request
from app.workers.tasks import process_jarvis_query

_ALLOWED_RESPONSE_URL_HOSTS = {
    "hooks.slack.com",
    "hooks-global.slack.com",
    "hooks.eu.slack.com",
}

router = APIRouter(prefix="/slack", tags=["slack"])

logger = logging.getLogger(__name__)


def _parse_scope_and_query(text: str) -> tuple[str | None, str]:
    """Parse an optional scope prefix from Slack slash-command text.

    Args:
        text: Raw slash-command text.

    Returns:
        tuple[str | None, str]: The scope name and the remaining query text.
    """
    stripped = (text or "").strip()
    if not stripped:
        return None, ""

    head, sep, tail = stripped.partition(" ")
    prefix = head.lower()

    if prefix in {"cf", "coldfusion"}:
        return "coldfusion", tail.strip() if sep else ""
    if prefix in {"py", "python"}:
        return "python", tail.strip() if sep else ""

    return None, stripped


async def _require_slack_signature(request: Request) -> None:
    """Validate the Slack request signature before reading form data.

    This dependency reads the raw request body so the later form parsing can safely
    consume the cached payload.

    Args:
        request: Incoming FastAPI request.

    Raises:
        HTTPException: If the Slack signature is invalid.
    """
    try:
        await verify_slack_request(request=request)
    except SlackVerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid request signature",
        ) from exc


@router.post("/commands")
async def slash_command(
    request: Request,
    _: None = Depends(_require_slack_signature),
) -> dict:
    """Handle a Slack slash command and enqueue the worker task.

    The function validates the request signature, reads the Slack form payload,
    rejects missing or invalid request data, and returns an ephemeral acknowledgement
    once the asynchronous task has been queued.

    Args:
        request: FastAPI request containing the Slack form payload.
        _: Dependency-injected signature validation result.

    Returns:
        dict: An ephemeral Slack acknowledgement payload.

    Raises:
        HTTPException: If required Slack fields are missing or the response URL is
            invalid.
    """
    # Parse form after verification — body is cached in request._body by the dependency
    form = await request.form()
    text = form.get("text", "")
    response_url = form.get("response_url", "")
    user_id = form.get("user_id", "")
    scope, query_text = _parse_scope_and_query(text)

    if not query_text:
        return {
            "response_type": "ephemeral",
            "text": "Please provide a question. Optional scope: 'cf' or 'py'.",
        }

    if not response_url or not user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required Slack fields: response_url and user_id",
        )

    _parsed = urlparse(response_url)
    if _parsed.scheme != "https" or _parsed.hostname not in _ALLOWED_RESPONSE_URL_HOSTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid response_url: must be an HTTPS Slack webhook URL",
        )

    # Check if the user is allowed
    if settings.slack_allowed_user_ids:
        allowed_ids = [
            uid.strip() for uid in settings.slack_allowed_user_ids.split(",")
        ]
        if user_id not in allowed_ids:
            return {
                "response_type": "ephemeral",
                "text": (
                    "I'm afraid I can't assist you with that, sir. "
                    "Access is restricted."
                ),
            }

    # The request's correlation ID becomes the Celery task ID, so the worker
    # (and Celery's own log lines) carry the same ID without extra plumbing.
    cid = correlation_id.get() or new_correlation_id()
    try:
        process_jarvis_query.apply_async(
            kwargs={
                "query": query_text,
                "response_url": response_url,
                "user_id": user_id,
                "scope": scope,
            },
            task_id=cid,
        )
    except Exception:
        # Still a 500 for Slack, as before. Logged here, inside the request,
        # so the line carries the correlation ID; the server's own traceback
        # line is written outside the middleware and has none.
        TASKS_ENQUEUED.labels(outcome="error").inc()
        logger.exception("failed to enqueue task")
        raise
    TASKS_ENQUEUED.labels(outcome="success").inc()
    logger.info("slash command accepted; task enqueued")

    # Must return within 3 seconds — heavy work happens in the Celery task
    return {
        "response_type": "ephemeral",
        "text": "One moment, sir. Gathering relevant context...",
    }
