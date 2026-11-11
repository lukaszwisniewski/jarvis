from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from prometheus_client import REGISTRY

from app.config import settings
from app.connectors.http_retry import request_with_retry
from app.llm import client as llm_client
from app.retrieval.models import RetrievalResult
from app.retrieval.retrieval_service import retrieve
from app.workers.tasks import process_jarvis_query


def sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def response(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("GET", "https://example.test"))


@pytest.mark.asyncio
@patch("app.retrieval.retrieval_service.ConfluenceProvider")
@patch("app.retrieval.retrieval_service.JiraProvider")
@patch("app.retrieval.retrieval_service.GitHubProvider")
async def test_source_latency_by_source_and_outcome(github, jira, confluence) -> None:
    github.return_value.search = AsyncMock(
        return_value=[RetrievalResult(source="github", title="a", snippet="", score=1)]
    )
    jira.return_value.search = AsyncMock(side_effect=RuntimeError("jira down"))
    confluence.return_value.search = AsyncMock(return_value=[])
    name = "jarvis_source_request_duration_seconds_count"
    before = {
        ("github", "success"): sample(name, source="github", outcome="success"),
        ("jira", "error"): sample(name, source="jira", outcome="error"),
        ("confluence", "success"): sample(name, source="confluence", outcome="success"),
    }

    results = await retrieve(["login"])

    assert [r.title for r in results] == ["a"]  # behaviour unchanged
    for (source, outcome), value in before.items():
        assert sample(name, source=source, outcome=outcome) == value + 1


@pytest.mark.asyncio
@patch("app.connectors.http_retry.asyncio.sleep", new_callable=AsyncMock)
async def test_http_retry_on_5xx_is_counted(mock_sleep) -> None:
    factory = AsyncMock(side_effect=[response(503), response(200)])
    before = sample("jarvis_http_retries_total", source="jira", reason="status_5xx")

    result = await request_with_retry(factory, source="jira")

    assert result.status_code == 200
    assert (
        sample("jarvis_http_retries_total", source="jira", reason="status_5xx")
        == before + 1
    )


@pytest.mark.asyncio
@patch("app.connectors.http_retry.asyncio.sleep", new_callable=AsyncMock)
async def test_timeouts_and_timeout_retries_are_counted(mock_sleep) -> None:
    factory = AsyncMock(side_effect=[httpx.ReadTimeout("slow"), response(200)])
    timeouts = sample("jarvis_http_timeouts_total", source="github")
    retries = sample("jarvis_http_retries_total", source="github", reason="timeout")

    await request_with_retry(factory, source="github")

    assert sample("jarvis_http_timeouts_total", source="github") == timeouts + 1
    assert (
        sample("jarvis_http_retries_total", source="github", reason="timeout")
        == retries + 1
    )


@pytest.mark.asyncio
async def test_final_timeout_counts_timeout_but_not_retry() -> None:
    factory = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
    timeouts = sample("jarvis_http_timeouts_total", source="confluence")
    retries = sample("jarvis_http_retries_total", source="confluence", reason="timeout")

    with pytest.raises(httpx.ReadTimeout):
        await request_with_retry(factory, source="confluence", max_retries=0)

    assert sample("jarvis_http_timeouts_total", source="confluence") == timeouts + 1
    assert (
        sample("jarvis_http_retries_total", source="confluence", reason="timeout")
        == retries
    )


@patch.object(settings, "llm_stub_mode", False)
def test_llm_duration_and_tokens_are_counted() -> None:
    message = SimpleNamespace(
        content=[SimpleNamespace(text="answer")],
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
    )
    fake = MagicMock()
    fake.messages.create.return_value = message
    before_in = sample("jarvis_llm_input_tokens_total", mode="synthesis")
    before_out = sample("jarvis_llm_output_tokens_total", mode="synthesis")
    before_ok = sample(
        "jarvis_llm_request_duration_seconds_count", mode="synthesis", outcome="success"
    )

    with patch.object(llm_client, "_get_client", return_value=fake):
        assert llm_client.ask_llm("question", context="ctx") == "answer"

    assert sample("jarvis_llm_input_tokens_total", mode="synthesis") == before_in + 11
    assert sample("jarvis_llm_output_tokens_total", mode="synthesis") == before_out + 7
    assert (
        sample(
            "jarvis_llm_request_duration_seconds_count",
            mode="synthesis",
            outcome="success",
        )
        == before_ok + 1
    )


@patch.object(settings, "llm_stub_mode", False)
def test_llm_error_is_counted() -> None:
    fake = MagicMock()
    fake.messages.create.side_effect = RuntimeError("api down")
    before = sample(
        "jarvis_llm_request_duration_seconds_count",
        mode="query_generation",
        outcome="error",
    )

    with patch.object(llm_client, "_get_client", return_value=fake):
        with pytest.raises(RuntimeError):
            llm_client.ask_llm("question", fast=True)

    assert (
        sample(
            "jarvis_llm_request_duration_seconds_count",
            mode="query_generation",
            outcome="error",
        )
        == before + 1
    )


@patch.object(settings, "agent_mode", False)
@patch("app.workers.tasks._build_context", side_effect=RuntimeError("retrieval broke"))
def test_task_failure_is_counted(mock_build_context) -> None:
    before = sample(
        "jarvis_task_duration_seconds_count", mode="pipeline", outcome="failure"
    )

    result = process_jarvis_query.apply(
        kwargs={
            "query": "q",
            "response_url": "https://hooks.slack.com/x",
            "user_id": "U1",
        }
    )

    assert result.failed()
    assert (
        sample("jarvis_task_duration_seconds_count", mode="pipeline", outcome="failure")
        == before + 1
    )


@pytest.mark.asyncio
async def test_agent_decide_duration_and_tokens_are_counted() -> None:
    from app.agent.graph import build_agent_graph

    fake = MagicMock()
    fake.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="done")],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=5, output_tokens=2),
    )
    before_in = sample("jarvis_llm_input_tokens_total", mode="agent_decide")
    before_ok = sample(
        "jarvis_llm_request_duration_seconds_count",
        mode="agent_decide",
        outcome="success",
    )

    graph = build_agent_graph(fake, model="fake", max_tool_rounds=1)
    await graph.ainvoke(
        {
            "question": "q",
            "scope": None,
            "messages": [{"role": "user", "content": "q"}],
            "results": [],
            "tool_rounds": 0,
            "context": "",
        }
    )

    assert sample("jarvis_llm_input_tokens_total", mode="agent_decide") == before_in + 5
    assert (
        sample(
            "jarvis_llm_request_duration_seconds_count",
            mode="agent_decide",
            outcome="success",
        )
        == before_ok + 1
    )
