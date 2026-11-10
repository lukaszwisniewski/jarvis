from unittest.mock import AsyncMock, patch

from app.config import settings
from app.workers.tasks import _build_context


# The default value itself is checked in test_agent_config.py without .env.
# Here the flag is patched explicitly, so a local .env cannot change the result.
@patch.object(settings, "agent_mode", False)
@patch("app.workers.tasks.agent_retrieve_context", new_callable=AsyncMock)
@patch("app.workers.tasks.retrieve_context", new_callable=AsyncMock)
def test_default_uses_fixed_pipeline(mock_pipeline, mock_agent) -> None:
    mock_pipeline.return_value = "pipeline context"

    assert _build_context("question", "python") == "pipeline context"
    mock_pipeline.assert_awaited_once_with("question", scope="python")
    mock_agent.assert_not_awaited()


@patch.object(settings, "agent_mode", True)
@patch("app.workers.tasks.agent_retrieve_context", new_callable=AsyncMock)
@patch("app.workers.tasks.retrieve_context", new_callable=AsyncMock)
def test_agent_mode_uses_agent(mock_pipeline, mock_agent) -> None:
    mock_agent.return_value = "agent context"

    assert _build_context("question", None) == "agent context"
    mock_agent.assert_awaited_once_with("question", scope=None)
    mock_pipeline.assert_not_awaited()
