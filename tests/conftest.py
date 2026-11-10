import os

import pytest
from fastapi.testclient import TestClient

# Must be set before app imports — Settings() is instantiated at module level
os.environ["SLACK_BOT_TOKEN"] = "xoxb-test"
os.environ["SLACK_SIGNING_SECRET"] = "test-signing-secret"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
os.environ["LLM_STUB_MODE"] = "true"

from app.main import app
from app.workers.celery_app import celery_app  # noqa: E402
from tests.log_capture import restored_logging  # noqa: E402

# Celery: use the in-memory broker and result backend for the whole session.
# This runs while conftest loads, before any test can create a backend,
# producer pool or broker connection (Celery creates those lazily), so no
# test ever sees the Redis URL from settings and nothing tries to reach Redis.
TEST_BROKER_URL = "memory://"
TEST_RESULT_BACKEND = "cache+memory://"
celery_app.conf.update(broker_url=TEST_BROKER_URL, result_backend=TEST_RESULT_BACKEND)


@pytest.fixture(autouse=True)
def celery_uses_in_memory_transport():
    """Fail fast if an earlier test changed Celery's broker or backend."""
    assert celery_app.conf.broker_url == TEST_BROKER_URL, (
        f"Celery broker_url was changed to {celery_app.conf.broker_url!r}; "
        f"tests must use {TEST_BROKER_URL!r}"
    )
    assert celery_app.conf.result_backend == TEST_RESULT_BACKEND, (
        f"Celery result_backend was changed to {celery_app.conf.result_backend!r}; "
        f"tests must use {TEST_RESULT_BACKEND!r}"
    )


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def restore_logging():
    """Undo any logging changes a test makes (root and configure_logging's)."""
    with restored_logging():
        yield
