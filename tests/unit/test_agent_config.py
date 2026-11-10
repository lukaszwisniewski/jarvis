import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.fixture
def make_settings(monkeypatch):
    """Build Settings without the local .env file or agent env vars."""
    monkeypatch.delenv("AGENT_MODE", raising=False)
    monkeypatch.delenv("AGENT_MAX_TOOL_ROUNDS", raising=False)
    return lambda **values: Settings(_env_file=None, **values)


def test_agent_mode_defaults(make_settings) -> None:
    settings = make_settings()

    assert settings.agent_mode is False
    assert settings.agent_max_tool_rounds == 4


@pytest.mark.parametrize("rounds", [1, 8])
def test_agent_max_tool_rounds_accepts_bounds(make_settings, rounds: int) -> None:
    assert make_settings(agent_max_tool_rounds=rounds).agent_max_tool_rounds == rounds


@pytest.mark.parametrize("rounds", [0, 9, 100])
def test_agent_max_tool_rounds_rejects_out_of_range(make_settings, rounds) -> None:
    with pytest.raises(ValidationError):
        make_settings(agent_max_tool_rounds=rounds)
