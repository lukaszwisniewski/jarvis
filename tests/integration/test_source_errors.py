"""Source failures through the real connectors and providers.

httpx.MockTransport answers every request with 401, like an expired token.
No request leaves the process.
"""

from unittest.mock import patch

import httpx
import pytest
from prometheus_client import REGISTRY

from app.config import settings
from app.connectors.confluence import ConfluenceConnector
from app.connectors.jira import JiraConnector
from app.retrieval.retrieval_service import retrieve


def sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
def every_request_unauthorized():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(401, json={"message": "Bad credentials"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    class MockedAsyncClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    with (
        patch.object(httpx, "AsyncClient", MockedAsyncClient),
        patch.object(JiraConnector, "JIRA_BASE_URL", "https://jira.example.test"),
        patch.object(
            ConfluenceConnector, "CONFLUENCE_BASE_URL", "https://wiki.example.test"
        ),
        patch.object(settings, "github_repos", ["demo/repo"]),
    ):
        yield seen


@pytest.mark.asyncio
async def test_failing_connectors_are_counted_as_query_errors(
    every_request_unauthorized,
) -> None:
    sources = ("github", "jira", "confluence")
    errors_before = {
        s: sample("jarvis_source_query_errors_total", source=s) for s in sources
    }
    success_before = {
        s: sample(
            "jarvis_source_request_duration_seconds_count", source=s, outcome="success"
        )
        for s in sources
    }

    results = await retrieve(["login token"])

    assert results == []
    assert len(every_request_unauthorized) == 3  # one search per source
    for source in sources:
        # The failed query is counted...
        assert sample("jarvis_source_query_errors_total", source=source) == (
            errors_before[source] + 1
        )
        # ...while the latency metric still says "success": the provider
        # caught the error, so no exception escaped it.
        assert (
            sample(
                "jarvis_source_request_duration_seconds_count",
                source=source,
                outcome="success",
            )
            == success_before[source] + 1
        )
