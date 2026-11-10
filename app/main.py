from fastapi import FastAPI, Request

from app.api.webhooks.slack import router as slack_router
from app.config import settings
from app.logging_config import configure_logging
from app.observability.context import (
    MAX_CLIENT_REQUEST_ID_LENGTH,
    client_request_id,
    correlation_id,
    new_correlation_id,
)

configure_logging(settings.log_level, settings.log_format)

app = FastAPI(title="Jarvis", version="0.1.0")


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    # Always generate our own ID: it becomes the Celery task ID, so it must
    # never come from the client. A client X-Request-ID is only logged.
    cid = new_correlation_id()
    client_id = request.headers.get("x-request-id")
    cid_token = correlation_id.set(cid)
    client_token = client_request_id.set(
        client_id[:MAX_CLIENT_REQUEST_ID_LENGTH] if client_id else None
    )
    try:
        response = await call_next(request)
    finally:
        correlation_id.reset(cid_token)
        client_request_id.reset(client_token)
    response.headers["X-Correlation-ID"] = cid
    return response


app.include_router(slack_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
