"""A fake GitHub / Jira / Confluence HTTP backend for offline evaluation.

The real connectors, providers and ranking code run unchanged. Only the HTTP
layer is replaced: every httpx.AsyncClient gets an httpx.MockTransport that
answers from the JSON corpus in eval/corpus/. Any URL the fake backend does
not know raises an error, so an eval run can never reach the real network.

Candidate selection is deliberately crude and stands in for the remote search
engines: a document matches when it contains at least one query token, and
matches are ordered by how many distinct query tokens they contain. The eval
therefore measures JARVIS's own query fallback, re-scoring, normalisation,
caps and ranking, not the quality of GitHub, Jira or Confluence search.
"""

import base64
import contextlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

import httpx

from app.config import settings
from app.connectors.confluence import ConfluenceConnector
from app.connectors.jira import JiraConnector

CORPUS_DIR = Path(__file__).parent / "corpus"
GITHUB_API = "https://api.github.com"
JIRA_BASE_URL = "https://jira.example.test"
CONFLUENCE_BASE_URL = "https://wiki.example.test"


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2}


def _rank(query: str, docs: list[dict], text_of, limit: int) -> list[dict]:
    """Return up to `limit` docs sharing tokens with the query, best first."""
    query_tokens = _tokens(query)
    scored = []
    for doc in docs:
        overlap = len(query_tokens & _tokens(text_of(doc)))
        if overlap:
            scored.append((overlap, doc))
    scored.sort(key=lambda pair: pair[0], reverse=True)  # stable for ties
    return [doc for _, doc in scored[:limit]]


def _quoted(expression: str) -> str:
    """Extract the search text from `text ~ "..."` in JQL or CQL."""
    match = re.search(r'text ~ "(.*?)"', expression)
    return match.group(1) if match else ""


class FakeBackend:
    def __init__(self, corpus_dir: Path = CORPUS_DIR) -> None:
        self.github = json.loads((corpus_dir / "github.json").read_text())
        self.jira = json.loads((corpus_dir / "jira.json").read_text())
        self.confluence = json.loads((corpus_dir / "confluence.json").read_text())
        self.repo = self.github["repo"]
        self.requests: list[str] = []

        # Maps each URL a RetrievalResult can carry back to a golden-set id.
        self.url_to_id: dict[str, str] = {}
        for f in self.github["files"]:
            self.url_to_id[self._html_url(f["path"])] = f"github:{f['path']}"
        for issue in self.jira["issues"]:
            url = f"{JIRA_BASE_URL}/browse/{issue['key']}"
            self.url_to_id[url] = f"jira:{issue['key']}"
        for page in self.confluence["pages"]:
            url = f"{CONFLUENCE_BASE_URL}{self._webui(page['id'])}"
            self.url_to_id[url] = f"confluence:{page['id']}"

    def _html_url(self, path: str) -> str:
        return f"https://github.com/{self.repo}/blob/main/{path}"

    def _contents_url(self, path: str) -> str:
        return f"{GITHUB_API}/repos/{self.repo}/contents/{path}"

    @staticmethod
    def _webui(page_id: str) -> str:
        return f"/wiki/spaces/ENG/pages/{page_id}"

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url.copy_with(query=None))
        params = request.url.params
        self.requests.append(url)

        if url == f"{GITHUB_API}/search/code":
            # Drop "repo:owner/name" qualifiers; they are not search text.
            text = " ".join(
                p for p in params.get("q", "").split() if not p.startswith("repo:")
            )
            files = _rank(
                text,
                self.github["files"],
                lambda f: f"{f['path']} {f['content']}",
                int(params.get("per_page", 15)),
            )
            items = [
                {
                    "path": f["path"],
                    "url": self._contents_url(f["path"]),
                    "html_url": self._html_url(f["path"]),
                }
                for f in files
            ]
            return httpx.Response(200, json={"total_count": len(items), "items": items})

        contents_prefix = f"{GITHUB_API}/repos/{self.repo}/contents/"
        if url.startswith(contents_prefix):
            path = unquote(url[len(contents_prefix) :])
            for f in self.github["files"]:
                if f["path"] == path:
                    encoded = base64.b64encode(f["content"].encode()).decode()
                    return httpx.Response(200, json={"content": encoded})
            return httpx.Response(404, json={})

        if url == f"{JIRA_BASE_URL}/rest/api/3/search/jql":
            issues = _rank(
                _quoted(params.get("jql", "")),
                self.jira["issues"],
                lambda i: f"{i['summary']} {i['description']}",
                int(params.get("maxResults", 5)),
            )
            return httpx.Response(
                200,
                json={
                    "issues": [
                        {
                            "key": i["key"],
                            "fields": {
                                "summary": i["summary"],
                                "description": i["description"],
                                "status": {"name": i["status"]},
                                "assignee": None,
                                "comment": None,
                            },
                        }
                        for i in issues
                    ]
                },
            )

        if url == f"{CONFLUENCE_BASE_URL}/wiki/rest/api/search":
            pages = _rank(
                _quoted(params.get("cql", "")),
                self.confluence["pages"],
                lambda p: f"{p['title']} {p['excerpt']} {p['body']}",
                int(params.get("limit", 5)),
            )
            results = [
                {
                    "content": {
                        "id": p["id"],
                        "title": p["title"],
                        "_links": {"webui": self._webui(p["id"])},
                        "space": {"name": self.confluence["space"]},
                        "version": {"number": 1},
                    },
                    "excerpt": p["excerpt"],
                }
                for p in pages
            ]
            return httpx.Response(200, json={"results": results})

        content_prefix = f"{CONFLUENCE_BASE_URL}/wiki/rest/api/content/"
        if url.startswith(content_prefix):
            page_id = url[len(content_prefix) :]
            for p in self.confluence["pages"]:
                if p["id"] == page_id:
                    return httpx.Response(
                        200, json={"body": {"storage": {"value": p["body"]}}}
                    )
            return httpx.Response(404, json={})

        # Fail loudly instead of silently returning nothing.
        raise AssertionError(f"fake backend received an unexpected URL: {url}")


@contextlib.contextmanager
def fake_search_backend(backend: FakeBackend | None = None) -> Iterator[FakeBackend]:
    """Route all connector HTTP traffic to a FakeBackend for the duration."""
    backend = backend or FakeBackend()
    transport = httpx.MockTransport(backend.handle)
    real_async_client = httpx.AsyncClient

    class _FakeAsyncClient(real_async_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    with contextlib.ExitStack() as stack:
        # Connectors look up httpx.AsyncClient at call time, so patching the
        # module attribute is enough to inject the mock transport everywhere.
        stack.enter_context(patch.object(httpx, "AsyncClient", _FakeAsyncClient))
        # Base URLs are class attributes read from settings at import time.
        stack.enter_context(patch.object(JiraConnector, "JIRA_BASE_URL", JIRA_BASE_URL))
        stack.enter_context(
            patch.object(
                ConfluenceConnector, "CONFLUENCE_BASE_URL", CONFLUENCE_BASE_URL
            )
        )
        stack.enter_context(patch.object(settings, "github_repos", [backend.repo]))
        stack.enter_context(patch.object(settings, "github_token", "eval-token"))
        # Stub mode makes query generation take its deterministic fallback,
        # so the eval is repeatable and needs no API key.
        stack.enter_context(patch.object(settings, "llm_stub_mode", True))
        yield backend
