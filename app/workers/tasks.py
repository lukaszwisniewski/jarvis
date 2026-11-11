import asyncio
import re
import time
from urllib.parse import urlparse

import httpx

from app.agent.graph import agent_retrieve_context
from app.config import settings
from app.llm.client import ask_llm
from app.llm.endpoint_inventory import build_endpoint_inventory_response
from app.observability.metrics import TASK_DURATION, TASK_RETRIES
from app.retrieval.pipeline import retrieve_context
from app.slack.formatting import format_response
from app.workers.celery_app import celery_app


def _source_counts_from_context(context: str) -> dict[str, int]:
    counts = {
        "github": 0,
        "jira": 0,
        "confluence": 0,
    }
    for line in context.splitlines():
        stripped = line.strip().upper()
        if stripped == "[GITHUB]":
            counts["github"] += 1
        elif stripped == "[JIRA]":
            counts["jira"] += 1
        elif stripped == "[CONFLUENCE]":
            counts["confluence"] += 1
    return counts


def _extract_github_repo(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except Exception:
        return None

    if "github.com" not in parsed.netloc:
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    return f"{parts[0]}/{parts[1]}"


def _source_breakdown_from_context(
    context: str,
) -> tuple[dict[str, int], int, int]:
    repo_counts: dict[str, int] = {}
    jira_count = 0
    confluence_count = 0
    current_source = ""

    for line in context.splitlines():
        stripped = line.strip()
        upper = stripped.upper()

        if upper == "[GITHUB]":
            current_source = "github"
            continue
        if upper == "[JIRA]":
            current_source = "jira"
            jira_count += 1
            continue
        if upper == "[CONFLUENCE]":
            current_source = "confluence"
            confluence_count += 1
            continue

        if current_source == "github" and stripped.startswith("URL: "):
            repo = _extract_github_repo(stripped.replace("URL: ", "", 1))
            if repo:
                repo_counts[repo] = repo_counts.get(repo, 0) + 1

    return repo_counts, jira_count, confluence_count


def _append_source_footer(answer: str, context: str) -> str:
    repo_counts, jira_count, confluence_count = _source_breakdown_from_context(context)

    parts: list[str] = []
    for repo, count in sorted(
        repo_counts.items(), key=lambda item: (-item[1], item[0])
    ):
        owner, name = repo.split("/", 1)
        parts.append(f"<https://github.com/{owner}/{name}|{name}> {count}")
    parts.append(f"Jira {jira_count}")
    parts.append(f"Confluence {confluence_count}")

    return answer + "\n\n" + f"_Source coverage: {' | '.join(parts)}_"


_CURRENT_STATE_PATTERNS = (
    r"\bcurrent\b",
    r"\bcurrently\b",
    r"\bnow\b",
    r"\bin use\b",
    r"\bwired\b",
    r"\bintegrat(ed|ion)\b",
    r"\bdeploy(ed|ment)?\b",
    r"\bactive\b",
)


def _is_current_state_query(query: str) -> bool:
    lowered = query.lower()
    return any(re.search(pattern, lowered) for pattern in _CURRENT_STATE_PATTERNS)


def _append_current_state_guardrail(answer: str, query: str, context: str) -> str:
    counts = _source_counts_from_context(context)
    if not _is_current_state_query(query):
        return answer

    if counts["github"] == 0:
        return (
            answer
            + "\n\n"
            + "_Current-state guardrail: no GitHub code evidence was retrieved. "
            + "Current wiring/deployment statements may be inferred "
            + "from non-code sources._"
        )

    if counts["github"] <= 1 and counts["jira"] > counts["github"]:
        return (
            answer
            + "\n\n"
            + "_Current-state guardrail: GitHub code evidence is limited; "
            + "verify current wiring/deployment from implementation files "
            + "before relying on this conclusion._"
        )

    return answer


def _build_context(query: str, scope: str | None) -> str:
    """Gather evidence for the question and return it as a context string.

    AGENT_MODE only changes how evidence is gathered; answer synthesis,
    guardrails and the source footer are the same for both paths.
    """
    if settings.agent_mode:
        return asyncio.run(agent_retrieve_context(query, scope=scope))
    return asyncio.run(retrieve_context(query, scope=scope))


def _slack_client() -> httpx.Client:
    """HTTP client for the Slack POST (a seam for tests to add a transport)."""
    return httpx.Client()


# Delay before retrying a failed Slack POST. A retry reruns the whole task.
SLACK_RETRY_COUNTDOWN_SECONDS = 5


def _retry(task, exc: Exception, reason: str) -> Exception:
    """Schedule a Celery retry and count it; returns the Retry to raise.

    When no retries are left, task.retry() raises `exc` itself, so nothing
    is counted and the task ends as a failure. A retry reruns every Claude
    call and can post a second answer if Slack accepted the first POST.
    """
    retry = task.retry(exc=exc, countdown=SLACK_RETRY_COUNTDOWN_SECONDS, throw=False)
    TASK_RETRIES.labels(reason).inc()
    return retry


# Nothing reads this task's result, so none is stored. Stored failure and
# retry records held the Slack response URL and its token (exc_message and
# traceback) in the result backend, where the log redaction does not apply.
@celery_app.task(bind=True, max_retries=2, ignore_result=True)
def process_jarvis_query(
    self,
    query: str,
    response_url: str,
    user_id: str,
    scope: str | None = None,
) -> None:
    """Run the JARVIS retrieval-and-response workflow for a Slack request.

    The task executes in the Celery worker, runs the async retrieval pipeline in a
    synchronous context, optionally uses the endpoint inventory response path, and
    posts the final formatted payload back to Slack.

    Args:
        self: Celery task instance.
        query: User question submitted via the slash command.
        response_url: Slack callback URL used for the deferred response.
        user_id: Slack user ID of the requester.
        scope: Optional retrieval scope passed through to the provider layer.

    Raises:
        CeleryRetry: Retries the task when the Slack post fails with a 5xx status,
            timeout, or network error.
    """
    mode = "agent" if settings.agent_mode else "pipeline"
    start = time.perf_counter()
    outcome = "failure"
    try:
        # Run the async retrieval (pipeline or agent) in a synchronous context
        context = _build_context(query, scope)
        answer = build_endpoint_inventory_response(query=query, context=context)
        if not answer:
            answer = ask_llm(query=query, context=context)
        answer = _append_current_state_guardrail(
            answer=answer, query=query, context=context
        )
        answer = _append_source_footer(answer=answer, context=context)
        payload = format_response(
            query=query,
            answer=answer,
            user_id=user_id,
        )

        with _slack_client() as client:
            response = client.post(url=response_url, json=payload, timeout=10)
            response.raise_for_status()
        outcome = "success"
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code < 500:
            raise
        retry = _retry(self, exc, "slack_5xx")
        outcome = "retry"
        raise retry
    except httpx.TimeoutException as exc:
        retry = _retry(self, exc, "slack_timeout")
        outcome = "retry"
        raise retry
    except httpx.NetworkError as exc:
        retry = _retry(self, exc, "slack_network")
        outcome = "retry"
        raise retry
    finally:
        TASK_DURATION.labels(mode, outcome).observe(time.perf_counter() - start)
