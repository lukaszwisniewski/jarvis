import logging
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.config import Settings, settings
from app.main import configure_api_logging
from app.observability.celery_signals import setup_worker_logging


@pytest.fixture(autouse=True)
def _restore(restore_logging):
    """Every test here reconfigures logging; undo it afterwards."""


def root_level() -> int:
    return logging.getLogger().level


ALLOWED_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
EXPECTED_ERROR = (
    "LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL "
    "(case-insensitive), or empty/unset"
)


@pytest.fixture
def make_settings(monkeypatch):
    """Build Settings without the local .env file or a LOG_LEVEL from the shell."""
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    return lambda **values: Settings(_env_file=None, **values)


def test_log_level_is_unset_by_default(monkeypatch) -> None:
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    assert Settings(_env_file=None).log_level is None


@pytest.mark.parametrize("name", ALLOWED_LEVELS)
def test_log_level_accepts_any_case(make_settings, name: str) -> None:
    for value in (name, name.lower(), name.capitalize()):
        assert make_settings(log_level=value).log_level == name


def test_log_level_from_env_is_uppercased(make_settings, monkeypatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "warning")
    assert make_settings().log_level == "WARNING"


def test_empty_log_level_means_not_configured(make_settings, monkeypatch) -> None:
    # LOG_LEVEL= in .env behaves like unset: each process keeps its default.
    monkeypatch.setenv("LOG_LEVEL", "")
    assert make_settings().log_level is None


@pytest.mark.parametrize(
    "value",
    [
        "VERBOSE",
        "WARN",
        "FATAL",
        "NOTSET",
        "10",
        "20",
        pytest.param(10, id="int-10"),
        " INFO ",
        "   ",
    ],
)
def test_log_level_rejects_other_values(make_settings, value) -> None:
    with pytest.raises(ValidationError) as excinfo:
        make_settings(log_level=value)
    message = str(excinfo.value)
    assert "log_level" in message
    assert EXPECTED_ERROR in message


@patch.object(settings, "log_level", "debug")
def test_log_level_set_wins_in_api_and_worker() -> None:
    configure_api_logging()
    assert root_level() == logging.DEBUG

    setup_worker_logging(loglevel=logging.INFO)  # Celery's --loglevel=info
    assert root_level() == logging.DEBUG


@patch.object(settings, "log_level", None)
def test_unset_worker_uses_celery_loglevel() -> None:
    setup_worker_logging(loglevel=logging.ERROR)
    assert root_level() == logging.ERROR


@patch.object(settings, "log_level", None)
def test_unset_api_uses_info() -> None:
    configure_api_logging()
    assert root_level() == logging.INFO
