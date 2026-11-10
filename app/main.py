import time

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.webhooks.slack import router as slack_router
from app.config import settings
from app.logging_config import configure_logging
from app.observability.context import (
    MAX_CLIENT_REQUEST_ID_LENGTH,
    client_request_id,
    correlation_id,
    new_correlation_id,
)
from app.observability.metrics import (
    API_REGISTRY,
    API_REQUEST_DURATION,
    status_class,
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
    start = time.perf_counter()
    outcome = "5xx"  # if call_next raises, the client gets a 500
    try:
        response = await call_next(request)
        outcome = status_class(response.status_code)
    finally:
        correlation_id.reset(cid_token)
        client_request_id.reset(client_token)
        # Scrapes of /metrics are not counted as API traffic.
        if request.url.path != "/metrics":
            API_REQUEST_DURATION.labels(outcome=outcome).observe(
                time.perf_counter() - start
            )
    response.headers["X-Correlation-ID"] = cid
    return response


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    # Only API metrics. Worker metrics are served by the worker on port 9100.
    return Response(generate_latest(API_REGISTRY), media_type=CONTENT_TYPE_LATEST)


app.include_router(slack_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
