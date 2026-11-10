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


def _changed_celery_settings() -> list[str]:
    """Describe each Celery transport setting that no longer has its test value."""
    expected = {
        "broker_url": TEST_BROKER_URL,
        "result_backend": TEST_RESULT_BACKEND,
    }
    return [
        f"Celery {name} was changed to {getattr(celery_app.conf, name)!r}; "
        f"tests must use {value!r}"
        for name, value in expected.items()
        if getattr(celery_app.conf, name) != value
    ]


@pytest.fixture(autouse=True)
def celery_uses_in_memory_transport():
    """Fail if a test changes Celery's broker or result backend.

    Before the test: catches a change made earlier (e.g. at import time).
    After the test: catches a change made by this test, so the failure is
    reported in the test that made it. The test values are put back first,
    so the following tests start clean.
    """
    changed = _changed_celery_settings()
    assert not changed, "; ".join(changed)
    yield
    changed = _changed_celery_settings()
    if changed:
        celery_app.conf.update(
            broker_url=TEST_BROKER_URL, result_backend=TEST_RESULT_BACKEND
        )
    assert not changed, "; ".join(changed)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def restore_logging():
    """Undo any logging changes a test makes (root and configure_logging's)."""
    with restored_logging():
        yield
