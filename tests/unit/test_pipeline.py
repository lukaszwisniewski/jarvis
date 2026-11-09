# tests/unit/test_pipeline.py

from unittest.mock import AsyncMock, patch

import pytest

import app.retrieval.pipeline as pipeline


@pytest.mark.asyncio
@patch("app.retrieval.pipeline.generate_queries")
@patch("app.retrieval.pipeline.retrieve", new_callable=AsyncMock)
@patch("app.retrieval.pipeline.build_context")
async def test_retrieve_context_happy_path(
    mock_build_context,
    mock_retrieve,
    mock_generate_queries,
):
    mock_generate_queries.return_value = [
        "thumbnail generation",
        "generate thumbnail",
    ]

    retrieved_documents = [object()]
    mock_retrieve.return_value = retrieved_documents

    mock_build_context.return_value = "context string"

    result = await pipeline.retrieve_context("test query")

    mock_generate_queries.assert_called_once_with("test query")

    mock_retrieve.assert_awaited_once_with(
        [
            "thumbnail generation",
            "generate thumbnail",
        ],
        scope=None,
    )

    mock_build_context.assert_called_once_with(retrieved_documents)

    assert result == "context string"


@pytest.mark.asyncio
@patch("app.retrieval.pipeline.generate_queries")
@patch("app.retrieval.pipeline.retrieve", new_callable=AsyncMock)
@patch("app.retrieval.pipeline.build_context")
async def test_retrieve_context_no_results_logs_warning(
    mock_build_context,
    mock_retrieve,
    mock_generate_queries,
    caplog,
):
    mock_generate_queries.return_value = [
        "thumbnail generation",
    ]

    mock_retrieve.return_value = []

    mock_build_context.return_value = ""

    with caplog.at_level("WARNING"):
        result = await pipeline.retrieve_context("test query")

    assert "retrieval returned no results" in caplog.text

    assert result == ""
