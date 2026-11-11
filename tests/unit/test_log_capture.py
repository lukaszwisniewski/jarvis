"""restored_logging() must leave loggers behaving as they did before.

Logger.isEnabledFor() caches its answers. Only setLevel() clears those
caches, so restoring a level by assigning .level directly would leave a
stale "DEBUG is enabled" answer behind after the context exits.
"""

import logging

import pytest

from tests.log_capture import restored_logging


@pytest.mark.parametrize("name", [None, "httpx"], ids=["root", "httpx"])
def test_restored_level_is_used_by_is_enabled_for(name: str | None) -> None:
    logger = logging.getLogger(name)
    original_level = logger.level
    try:
        # A known starting level above DEBUG, whatever earlier tests left.
        logger.setLevel(logging.WARNING)
        assert not logger.isEnabledFor(logging.DEBUG)

        with restored_logging():
            logger.setLevel(logging.DEBUG)
            assert logger.isEnabledFor(logging.DEBUG)  # cached as True

        assert logger.level == logging.WARNING
        assert not logger.isEnabledFor(logging.DEBUG)
    finally:
        # Do not rely on the helper under test; setLevel() also clears caches.
        logger.setLevel(original_level)
