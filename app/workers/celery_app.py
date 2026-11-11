from celery import Celery

# Connect logging and correlation-ID signal handlers.
import app.observability.celery_signals  # noqa: F401
from app.config import settings

celery_app = Celery(
    "jarvis",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
)
