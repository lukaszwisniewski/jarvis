import os

import pytest
from fastapi.testclient import TestClient

# Must be set before app imports — Settings() is instantiated at module level
os.environ["SLACK_BOT_TOKEN"] = "xoxb-test"
os.environ["SLACK_SIGNING_SECRET"] = "test-signing-secret"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
os.environ["LLM_STUB_MODE"] = "true"

from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
