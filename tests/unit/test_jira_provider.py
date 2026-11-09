from unittest.mock import AsyncMock

import pytest

from app.retrieval.providers.jira_provider import JiraProvider


def _issue(
    key: str,
    summary: str,
    description: str,
    comment_text: str = "",
) -> dict:
    comments = []
    if comment_text:
        comments.append(
            {
                "author": {"displayName": "Alice"},
                "body": comment_text,
            }
        )

    return {
        "key": key,
        "fields": {
            "summary": summary,
            "description": description,
            "status": {"name": "In Progress"},
            "assignee": {"displayName": "Bob"},
            "comment": {"comments": comments},
        },
    }


@pytest.mark.asyncio
async def test_search_dedupes_scores_and_includes_comments() -> None:
    provider = JiraProvider()
    provider.connector.search = AsyncMock(
        side_effect=[
            [
                _issue(
                    key="ENG-1",
                    summary="Thumbnail generation failure",
                    description="Service throws timeout in generation path",
                    comment_text="Looks related to queue latency",
                )
            ],
            [
                _issue(
                    key="ENG-1",
                    summary="Thumbnail generation failure",
                    description="Duplicate hit from another query",
                )
            ],
        ]
    )

    results = await provider.search(["thumbnail generation", "queue timeout"])

    assert len(results) == 1
    result = results[0]

    assert result.source == "jira"
    assert result.title == "Thumbnail generation failure"
    assert result.url.endswith("/browse/ENG-1")
    assert result.score is not None and result.score > 0
    assert "Comments:" in result.snippet


@pytest.mark.asyncio
async def test_search_continues_when_a_query_fails() -> None:
    provider = JiraProvider()
    provider.connector.search = AsyncMock(
        side_effect=[
            Exception("jira temporary failure"),
            [
                _issue(
                    key="ENG-2",
                    summary="Worker retry bug",
                    description="Celery retry loop",
                )
            ],
        ]
    )

    results = await provider.search(["one", "two"])

    assert len(results) == 1
    assert results[0].title == "Worker retry bug"
