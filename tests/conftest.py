import os

import pytest
from fastapi.testclient import TestClient

# Must be set before app imports — Settings() is instantiated at module level
os.environ["SLACK_BOT_TOKEN"] = "xoxb-test"
os.environ["SLACK_SIGNING_SECRET"] = "test-signing-secret"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
os.environ["LLM_STUB_MODE"] = "true"

from app.main import app
from tests.log_capture import restored_logging  # noqa: E402


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def restore_logging():
    """Undo any logging changes a test makes (root and configure_logging's)."""
    with restored_logging():
        yield
