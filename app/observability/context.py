"""Per-request and per-task context that is added to every log line."""

import uuid
from contextvars import ContextVar

# The API generates this ID per HTTP request and uses it as the Celery task
# ID, so the same value identifies the request, the task and all their logs.
correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

# An X-Request-ID sent by a client. It is untrusted, so it is only logged as
# a separate field and never used as the correlation or task ID.
client_request_id: ContextVar[str | None] = ContextVar(
    "client_request_id", default=None
)

MAX_CLIENT_REQUEST_ID_LENGTH = 128


def new_correlation_id() -> str:
    return str(uuid.uuid4())
