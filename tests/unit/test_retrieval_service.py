import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.retrieval.models import RetrievalResult
from app.retrieval.retrieval_service import _normalize_scores, retrieve


def test_normalize_scores_scales_to_zero_one_range() -> None:
    results = [
        RetrievalResult(source="github", title="a", snippet="", score=2.0),
        RetrievalResult(source="github", title="b", snippet="", score=6.0),
        RetrievalResult(source="github", title="c", snippet="", score=10.0),
    ]

    normalized = _normalize_scores(results)

    assert normalized[0].score == 0.0
    assert normalized[1].score == 0.5
    assert normalized[2].score == 1.0


def test_normalize_scores_uniform_batch_sets_one() -> None:
    results = [
        RetrievalResult(source="jira", title="a", snippet="", score=4.0),
        RetrievalResult(source="jira", title="b", snippet="", score=4.0),
    ]

    normalized = _normalize_scores(results)

    assert normalized[0].score == 1.0
    assert normalized[1].score == 1.0


@pytest.mark.asyncio
@patch("app.retrieval.retrieval_service.GitHubProvider")
@patch("app.retrieval.retrieval_service.JiraProvider")
async def test_retrieve_normalizes_biases_and_sorts(
    mock_jira_provider_cls,
    mock_github_provider_cls,
) -> None:
    mock_github_provider = mock_github_provider_cls.return_value
    mock_jira_provider = mock_jira_provider_cls.return_value

    mock_github_provider.search = AsyncMock(
        return_value=[
            RetrievalResult(source="github", title="gh-low", snippet="", score=10.0),
            RetrievalResult(source="github", title="gh-high", snippet="", score=20.0),
        ]
    )
    mock_jira_provider.search = AsyncMock(
        return_value=[
            RetrievalResult(source="jira", title="jira-low", snippet="", score=5.0),
            RetrievalResult(source="jira", title="jira-high", snippet="", score=9.0),
        ]
    )

    results = await retrieve(["thumbnail generation"])

    by_title = {item.title: item.score for item in results}

    assert by_title["gh-low"] == 0.3
    assert by_title["gh-high"] == 1.0
    assert by_title["jira-low"] == 0.0
    assert by_title["jira-high"] == 1.0

    assert results[0].source == "github"
    assert results[0].title == "gh-high"


@pytest.mark.asyncio
@patch("app.retrieval.retrieval_service.GitHubProvider")
@patch("app.retrieval.retrieval_service.JiraProvider")
@patch("app.retrieval.retrieval_service.ConfluenceProvider")
async def test_retrieve_returns_results_from_successful_providers_when_one_fails(
    mock_confluence_provider_cls,
    mock_jira_provider_cls,
    mock_github_provider_cls,
) -> None:
    mock_github_provider_cls.return_value.search = AsyncMock(
        side_effect=RuntimeError("github outage")
    )
    mock_jira_provider_cls.return_value.search = AsyncMock(
        return_value=[
            RetrievalResult(source="jira", title="jira-result", snippet="", score=7.0),
        ]
    )
    mock_confluence_provider_cls.return_value.search = AsyncMock(
        return_value=[
            RetrievalResult(
                source="confluence",
                title="confluence-result",
                snippet="",
                score=3.0,
            ),
        ]
    )

    results = await retrieve(["thumbnail generation"])

    assert [item.title for item in results] == ["jira-result", "confluence-result"]


@pytest.mark.asyncio
@patch("app.retrieval.retrieval_service.GitHubProvider")
@patch("app.retrieval.retrieval_service.JiraProvider")
@patch("app.retrieval.retrieval_service.ConfluenceProvider")
async def test_retrieve_returns_empty_list_when_all_providers_fail(
    mock_confluence_provider_cls,
    mock_jira_provider_cls,
    mock_github_provider_cls,
) -> None:
    for mock_cls in (
        mock_github_provider_cls,
        mock_jira_provider_cls,
        mock_confluence_provider_cls,
    ):
        mock_cls.return_value.search = AsyncMock(
            side_effect=RuntimeError("provider outage")
        )

    results = await retrieve(["thumbnail generation"])

    assert results == []


@pytest.mark.asyncio
@patch("app.retrieval.retrieval_service.GitHubProvider")
@patch("app.retrieval.retrieval_service.JiraProvider")
@patch("app.retrieval.retrieval_service.ConfluenceProvider")
async def test_retrieve_propagates_cancelled_error(
    mock_confluence_provider_cls,
    mock_jira_provider_cls,
    mock_github_provider_cls,
) -> None:
    mock_github_provider_cls.return_value.search = AsyncMock(
        side_effect=asyncio.CancelledError()
    )
    mock_jira_provider_cls.return_value.search = AsyncMock(
        return_value=[
            RetrievalResult(source="jira", title="jira-result", snippet="", score=1.0)
        ]
    )
    mock_confluence_provider_cls.return_value.search = AsyncMock(return_value=[])

    with pytest.raises(asyncio.CancelledError):
        await retrieve(["thumbnail generation"])


@pytest.mark.asyncio
@patch("app.retrieval.retrieval_service.JiraProvider")
async def test_retrieve_from_source_searches_only_that_source(
    mock_jira_provider_cls,
) -> None:
    from app.retrieval.retrieval_service import retrieve_from_source

    expected = [RetrievalResult(source="jira", title="t", snippet="", score=1.0)]
    mock_jira_provider_cls.return_value.search = AsyncMock(return_value=expected)

    assert await retrieve_from_source("jira", ["login"]) == expected
    mock_jira_provider_cls.return_value.search.assert_awaited_once_with(["login"])


@pytest.mark.asyncio
@patch("app.retrieval.retrieval_service.JiraProvider")
async def test_retrieve_from_source_raises_provider_errors(
    mock_jira_provider_cls,
) -> None:
    from app.retrieval.retrieval_service import retrieve_from_source

    mock_jira_provider_cls.return_value.search = AsyncMock(
        side_effect=RuntimeError("jira down")
    )

    with pytest.raises(RuntimeError, match="jira down"):
        await retrieve_from_source("jira", ["login"])


@pytest.mark.asyncio
async def test_retrieve_from_source_rejects_unknown_source() -> None:
    from app.retrieval.retrieval_service import retrieve_from_source

    with pytest.raises(ValueError, match="unknown retrieval source"):
        await retrieve_from_source("slack", ["login"])


def test_rank_results_applies_source_caps() -> None:
    from app.retrieval.retrieval_service import rank_results

    confluence = [
        RetrievalResult(source="confluence", title=f"p{i}", snippet="", score=i)
        for i in range(3)
    ]

    ranked = rank_results({"confluence": confluence})

    # Default cap keeps one Confluence page: the best-scoring one.
    assert [r.title for r in ranked] == ["p2"]
