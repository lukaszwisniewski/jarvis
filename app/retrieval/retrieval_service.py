import asyncio
import logging

from app.config import settings
from app.retrieval.models import RetrievalResult
from app.retrieval.providers.confluence_provider import ConfluenceProvider
from app.retrieval.providers.github_provider import GitHubProvider
from app.retrieval.providers.jira_provider import JiraProvider

logger = logging.getLogger(__name__)


def _normalize_scores(results: list[RetrievalResult]) -> list[RetrievalResult]:
    """Rescale a provider result batch to a 0..1 score range.

    Args:
        results: Retrieval results for a single provider.

    Returns:
        list[RetrievalResult]: The same list with normalized scores in place. Any
        result without a numeric score is left unchanged.
    """
    scored = [r for r in results if r.score is not None]
    if not scored:
        return results
    lo = min(r.score for r in scored)
    hi = max(r.score for r in scored)
    if hi == lo:
        for r in scored:
            r.score = 1.0  # singleton or uniform batch — treat as best available
        return results
    for r in scored:
        r.score = (r.score - lo) / (hi - lo)
    return results


_GITHUB_BIAS = (
    0.3  # encodes system-prompt preference: GitHub preferred for code answers
)


def _source_caps() -> dict[str, int]:
    caps = settings.retrieval_source_caps or {}
    return {
        "github": max(0, int(caps.get("github", 6))),
        "jira": max(0, int(caps.get("jira", 3))),
        "confluence": max(0, int(caps.get("confluence", 1))),
    }


async def retrieve(
    queries: list[str],
    scope: str | None = None,
) -> list[RetrievalResult]:
    """Fetch and rank retrieval results from all configured providers.

    The GitHub, Jira, and Confluence providers run concurrently. Ordinary provider
    exceptions are logged and ignored so successful providers can still contribute
    results; `asyncio.CancelledError` is re-raised to preserve cancellation.

    Args:
        queries: Search terms passed to each provider.
        scope: Optional GitHub profile scope, such as "coldfusion" or "python".

    Returns:
        list[RetrievalResult]: Ranked results capped by provider limits and reduced
        to the top ten items.
    """
    source_caps = _source_caps()

    github_provider = GitHubProvider(scope=scope)
    jira_provider = JiraProvider()
    confluence_provider = ConfluenceProvider()

    provider_results = await asyncio.gather(
        github_provider.search(queries),
        jira_provider.search(queries),
        confluence_provider.search(queries),
        return_exceptions=True,
    )

    github_results: list[RetrievalResult] = []
    jira_results: list[RetrievalResult] = []
    confluence_results: list[RetrievalResult] = []

    for provider_name, result in (
        ("github", provider_results[0]),
        ("jira", provider_results[1]),
        ("confluence", provider_results[2]),
    ):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, Exception):
            logger.warning(
                "retrieval provider failed: provider=%s error=%s",
                provider_name,
                result,
            )
            continue
        if provider_name == "github":
            github_results = result
        elif provider_name == "jira":
            jira_results = result
        else:
            confluence_results = result

    if not github_results and not jira_results and not confluence_results:
        logger.warning("all retrieval providers failed for query set=%s", queries)

    github_results = _normalize_scores(github_results)
    jira_results = _normalize_scores(jira_results)
    confluence_results = _normalize_scores(confluence_results)

    # Apply bias: shifts GitHub range from [0,1] to [0.3, 1.0] (capped)
    # A highly relevant Jira result (>0.3) can still outrank a weak GitHub result
    for r in github_results:
        r.score = min(1.0, (r.score or 0.0) + _GITHUB_BIAS)

    github_results.sort(
        key=lambda x: x.score if x.score is not None else 0.0, reverse=True
    )
    jira_results.sort(
        key=lambda x: x.score if x.score is not None else 0.0, reverse=True
    )
    confluence_results.sort(
        key=lambda x: x.score if x.score is not None else 0.0,
        reverse=True,
    )

    capped_results = (
        github_results[: source_caps["github"]]
        + jira_results[: source_caps["jira"]]
        + confluence_results[: source_caps["confluence"]]
    )
    capped_results.sort(
        key=lambda x: x.score if x.score is not None else 0.0,
        reverse=True,
    )

    return capped_results[:10]
