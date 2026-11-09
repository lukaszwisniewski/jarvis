import httpx
import pytest

from app.config import settings
from app.connectors.github import GitHubConnector
from app.connectors.http_retry import request_with_retry


@pytest.mark.asyncio
async def test_build_snippet_prefers_high_density_keyword_window() -> None:
    connector = GitHubConnector()

    content = (
        "thumbnail appears once early.\n"
        + ("padding text " * 80)
        + "generation pipeline service handles thumbnail generation service quickly.\n"
        + ("tail text " * 20)
    )

    snippet = await connector.build_snippet(
        content=content,
        queries=["thumbnail generation service"],
        window=80,
    )

    assert "generation" in snippet.lower()
    assert "service" in snippet.lower()


@pytest.mark.asyncio
async def test_build_snippet_falls_back_when_no_usable_keywords() -> None:
    connector = GitHubConnector()

    first_line = "first line"
    second_line = "second line"
    content = f"{first_line}\n{second_line}\n" + ("x" * 1200)

    snippet = await connector.build_snippet(
        content=content,
        queries=["an to if"],
    )

    assert snippet == f"{first_line}\n{second_line}"


@pytest.mark.asyncio
async def test_build_snippet_falls_back_when_keywords_not_present() -> None:
    connector = GitHubConnector()

    content = "alpha beta\ngamma delta\n" + ("z" * 1200)

    snippet = await connector.build_snippet(
        content=content,
        queries=["thumbnail generation"],
    )

    assert snippet == "alpha beta\ngamma delta"


@pytest.mark.asyncio
async def test_search_token_normalization_matches_plural_query_to_singular_path(
    monkeypatch,
) -> None:
    connector = GitHubConnector()

    # Make extension scoring neutral so path-token boost drives ordering.
    monkeypatch.setattr(settings, "github_allowed_extensions", [".cfc"])
    monkeypatch.setattr(settings, "github_extension_scores", {".cfc": 1})

    class FakeResponse:
        def __init__(self, payload: dict):
            self._payload = payload
            self.status_code = 200
            self.headers = {}
            self.request = httpx.Request("GET", "https://example.com")

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    captured = {}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None, params=None):
            captured["params"] = params
            return FakeResponse(
                {
                    "total_count": 2,
                    "items": [
                        {
                            "path": "components/api/orderService.cfc",
                            "html_url": "https://github.com/example-org/example-repo/blob/main/components/api/orderService.cfc",
                            "url": "https://api.github.com/repos/example-org/example-repo/contents/components/api/orderService.cfc",
                        },
                        {
                            "path": "components/api/unrelated.cfc",
                            "html_url": "https://github.com/example-org/example-repo/blob/main/components/api/unrelated.cfc",
                            "url": "https://api.github.com/repos/example-org/example-repo/contents/components/api/unrelated.cfc",
                        },
                    ],
                }
            )

    monkeypatch.setattr("app.connectors.github.httpx.AsyncClient", FakeAsyncClient)

    results = await connector.search(
        queries=["main files involved with orders"],
        repos=["example-org/example-repo"],
    )

    assert "repo:example-org/example-repo" in captured["params"]["q"]
    assert results[0]["path"] == "components/api/orderService.cfc"


@pytest.mark.asyncio
async def test_search_handles_empty_repo_list_without_repo_filter(monkeypatch) -> None:
    connector = GitHubConnector()
    captured = {}

    class FakeResponse:
        def __init__(self, payload: dict):
            self._payload = payload
            self.status_code = 200
            self.headers = {}
            self.request = httpx.Request("GET", "https://example.com")

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None, params=None):
            captured["params"] = params
            return FakeResponse(
                {
                    "total_count": 1,
                    "items": [
                        {
                            "path": "docs/example.md",
                            "html_url": "https://github.com/example-org/example-repo/blob/main/docs/example.md",
                            "url": "https://api.github.com/repos/example-org/example-repo/contents/docs/example.md",
                        }
                    ],
                }
            )

    monkeypatch.setattr("app.connectors.github.httpx.AsyncClient", FakeAsyncClient)

    results = await connector.search(
        queries=["example docs"],
        repos=[],
    )

    assert "repo:" not in captured["params"]["q"]
    assert results[0]["path"] == "docs/example.md"


@pytest.mark.asyncio
async def test_build_snippet_definition_query_prefers_class_anchor() -> None:
    connector = GitHubConnector()

    content = (
        "header text\n"
        + ("noise " * 200)
        + "class CreateReportRequest(BaseModel):\n"
        + "    file_name: str\n"
        + "    export_type: str\n"
    )

    snippet = await connector.build_snippet(
        content=content,
        queries=["what are the fields in class CreateReportRequest"],
        path="dtos.py",
        window=120,
    )

    assert "CreateReportRequest" in snippet
    assert "file_name" in snippet


@pytest.mark.asyncio
async def test_definition_snippet_prefers_class_block_until_next_class() -> None:
    connector = GitHubConnector()

    content = """
class ReportDTOs:
    class CreateReportRequest(BaseModel):
        report_id: UUID | None = None
        format: Literal["csv", "excel"]
        email: bool = False

    class AnotherInnerDTO(BaseModel):
        sample: bool = True
""".strip()

    snippet = await connector.build_snippet(
        content=content,
        queries=["fields in class CreateReportRequest"],
        path="dtos.py",
        window=120,
    )

    assert "class CreateReportRequest" in snippet
    assert "report_id" in snippet
    assert "AnotherInnerDTO" not in snippet


@pytest.mark.asyncio
async def test_build_snippet_definition_query_falls_back_when_anchor_missing() -> None:
    connector = GitHubConnector()

    content = (
        "thumbnail appears once early\n"
        + ("padding text " * 80)
        + "generation pipeline service handles thumbnail generation quickly\n"
    )

    snippet = await connector.build_snippet(
        content=content,
        queries=["what are fields in class MissingDto"],
        path="service.py",
        window=80,
    )

    # No class anchor present, should still return non-empty fallback snippet.
    assert snippet


@pytest.mark.asyncio
async def test_request_with_retry_retries_timeout_then_succeeds(monkeypatch) -> None:
    attempts = {"count": 0}
    sleep_calls = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    async def fake_request() -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise httpx.TimeoutException("slow")

        response = httpx.Response(
            200, request=httpx.Request("GET", "https://example.com")
        )
        return response

    monkeypatch.setattr("app.connectors.http_retry.asyncio.sleep", fake_sleep)

    response = await request_with_retry(fake_request, max_retries=2)

    assert response.status_code == 200
    assert attempts["count"] == 2
    assert sleep_calls


@pytest.mark.asyncio
async def test_request_with_retry_uses_retry_after_header(monkeypatch) -> None:
    attempts = {"count": 0}
    sleep_calls = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    def response_for_status_429() -> httpx.Response:
        request = httpx.Request("GET", "https://example.com")
        return httpx.Response(
            429,
            request=request,
            headers={"Retry-After": "2"},
        )

    async def fake_request() -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return response_for_status_429()
        return httpx.Response(200, request=httpx.Request("GET", "https://example.com"))

    monkeypatch.setattr("app.connectors.http_retry.asyncio.sleep", fake_sleep)

    response = await request_with_retry(fake_request, max_retries=2)

    assert response.status_code == 200
    assert any(delay == 2.0 for delay in sleep_calls)


@pytest.mark.asyncio
async def test_request_with_retry_retries_503_then_succeeds(monkeypatch) -> None:
    attempts = {"count": 0}
    sleep_calls = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    async def fake_request() -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            request = httpx.Request("GET", "https://example.com")
            response = httpx.Response(503, request=request)
            response.raise_for_status()
            return response
        return httpx.Response(200, request=httpx.Request("GET", "https://example.com"))

    monkeypatch.setattr("app.connectors.http_retry.asyncio.sleep", fake_sleep)

    response = await request_with_retry(fake_request, max_retries=2)

    assert response.status_code == 200
    assert attempts["count"] == 2
    assert sleep_calls


@pytest.mark.asyncio
async def test_request_with_retry_retries_network_error_then_succeeds(
    monkeypatch,
) -> None:
    attempts = {"count": 0}
    sleep_calls = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    async def fake_request() -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.NetworkError("temporary outage")
        return httpx.Response(200, request=httpx.Request("GET", "https://example.com"))

    monkeypatch.setattr("app.connectors.http_retry.asyncio.sleep", fake_sleep)

    response = await request_with_retry(fake_request, max_retries=2)

    assert response.status_code == 200
    assert attempts["count"] == 2
    assert sleep_calls


@pytest.mark.asyncio
async def test_request_with_retry_exhausts_retries_for_timeout(monkeypatch) -> None:
    calls = {"count": 0}

    async def fake_sleep(delay: float) -> None:
        return None

    async def fake_request() -> httpx.Response:
        calls["count"] += 1
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr("app.connectors.http_retry.asyncio.sleep", fake_sleep)

    with pytest.raises(httpx.TimeoutException):
        await request_with_retry(fake_request, max_retries=2)

    assert calls["count"] == 3


@pytest.mark.asyncio
async def test_request_with_retry_does_not_retry_ordinary_4xx(monkeypatch) -> None:
    calls = {"count": 0}

    async def fake_sleep(delay: float) -> None:
        raise AssertionError("sleep should not be called for ordinary 4xx")

    async def fake_request() -> httpx.Response:
        calls["count"] += 1
        request = httpx.Request("GET", "https://example.com")
        response = httpx.Response(400, request=request)
        response.raise_for_status()
        return response

    monkeypatch.setattr("app.connectors.http_retry.asyncio.sleep", fake_sleep)

    with pytest.raises(httpx.HTTPStatusError):
        await request_with_retry(fake_request, max_retries=2)

    assert calls["count"] == 1
