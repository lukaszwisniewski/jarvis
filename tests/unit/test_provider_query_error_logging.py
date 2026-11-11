"""Jira/Confluence query failures: WARNING names the error, DEBUG has details.

An httpx.HTTPStatusError message contains the request URL, and for these
sources the URL carries the JQL/CQL built from the user's question.
"""

import logging
from unittest.mock import AsyncMock

import httpx
import pytest
from prometheus_client import REGISTRY

from app.retrieval.providers.confluence_provider import ConfluenceProvider
from app.retrieval.providers.jira_provider import JiraProvider

SECRET = "SECRET-QUERY-TEXT"


def _status_error() -> httpx.HTTPStatusError:
    request = httpx.Request(
        "GET", f"https://example.atlassian.net/rest/api/3/search?jql={SECRET}"
    )
    response = httpx.Response(401, request=request)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("raise_for_status() did not raise")


def _record_text(record: logging.LogRecord) -> str:
    """Everything a handler could print for this record."""
    parts = [str(record.msg), record.getMessage(), repr(record.args)]
    if record.exc_info:
        parts.append(logging.Formatter().formatException(record.exc_info))
    if record.exc_text:
        parts.append(record.exc_text)
    return "\n".join(parts)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_cls", "source"),
    [(JiraProvider, "jira"), (ConfluenceProvider, "confluence")],
)
async def test_query_failure_keeps_query_text_out_of_warning(
    provider_cls, source, caplog
) -> None:
    error = _status_error()
    assert SECRET in str(error)  # the precondition this test is about

    provider = provider_cls()
    provider.connector.search = AsyncMock(side_effect=error)
    errors_before = (
        REGISTRY.get_sample_value(
            "jarvis_source_query_errors_total", {"source": source}
        )
        or 0.0
    )

    with caplog.at_level(logging.DEBUG):
        results = await provider.search(["how does login work"])

    assert results == []
    own = [r for r in caplog.records if r.name == provider_cls.__module__]

    # (a) no record at WARNING or above, from any logger, has the query text
    for record in caplog.records:
        if record.levelno >= logging.WARNING:
            assert SECRET not in _record_text(record)

    # (b) the warning names the error type
    loud = [r for r in own if r.levelno >= logging.WARNING]
    assert [r.getMessage() for r in loud] == [f"{source} query failed: HTTPStatusError"]

    # (c) the error counter still goes up by exactly one
    assert (
        REGISTRY.get_sample_value(
            "jarvis_source_query_errors_total", {"source": source}
        )
        == errors_before + 1
    )

    # (d) the details are still available at DEBUG
    debug = [r for r in own if r.levelno == logging.DEBUG]
    assert any(SECRET in _record_text(r) for r in debug)
