import logging
from unittest.mock import patch

import pytest

from app.config import Settings, settings
from app.main import configure_api_logging
from app.observability.celery_signals import setup_worker_logging


@pytest.fixture(autouse=True)
def _restore(restore_logging):
    """Every test here reconfigures logging; undo it afterwards."""


def root_level() -> int:
    return logging.getLogger().level


def test_log_level_is_unset_by_default(monkeypatch) -> None:
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    assert Settings(_env_file=None).log_level is None


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
