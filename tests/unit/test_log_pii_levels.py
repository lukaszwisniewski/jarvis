import logging
from unittest.mock import AsyncMock, patch

import pytest

import app.retrieval.pipeline as pipeline
from app.retrieval.models import RetrievalResult

USER_TEXT = ("query-text-from-user", "TITLE-from-jira", "SNIPPET-from-page")


@pytest.mark.asyncio
@patch("app.retrieval.pipeline.generate_queries")
@patch("app.retrieval.pipeline.retrieve", new_callable=AsyncMock)
async def test_pipeline_logs_user_text_only_at_debug(
    mock_retrieve, mock_generate_queries, caplog
) -> None:
    mock_generate_queries.return_value = ["query-text-from-user"]
    mock_retrieve.return_value = [
        RetrievalResult(
            source="jira", title="TITLE-from-jira", snippet="SNIPPET-from-page"
        )
    ]

    with caplog.at_level(logging.DEBUG):
        await pipeline.retrieve_context("question")

    info_and_above = [
        r.getMessage() for r in caplog.records if r.levelno >= logging.INFO
    ]
    debug_only = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    for text in USER_TEXT:
        assert not any(text in message for message in info_and_above), text
        assert any(text in message for message in debug_only), text


@pytest.mark.asyncio
@patch("app.retrieval.pipeline.generate_queries")
@patch("app.retrieval.pipeline.retrieve", new_callable=AsyncMock)
async def test_no_results_warning_has_count_not_queries(
    mock_retrieve, mock_generate_queries, caplog
) -> None:
    mock_generate_queries.return_value = ["query-text-from-user"]
    mock_retrieve.return_value = []

    with caplog.at_level(logging.WARNING):
        await pipeline.retrieve_context("question")

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["retrieval returned no results for 1 queries"]
