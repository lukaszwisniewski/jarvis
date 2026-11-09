import logging

from app.retrieval.models import RetrievalResult

logger = logging.getLogger(__name__)


def build_context(results: list[RetrievalResult]) -> str:
    """
    Formats a list of RetrievalResult objects into a context string for LLM synthesis.
    Each result is rendered with its source, title, snippet, and optional URL.
    Input is a list of results for a set of queries.
    """
    if not results:
        return ""

    lines = []

    for result in results:
        lines.append(f"[{result.source.upper()}]")
        lines.append(f"Title: {result.title}")
        lines.append(f"Snippet: {result.snippet}")

        if result.url:
            lines.append(f"URL: {result.url}")

        lines.append(f"Relevance: {result.score}")

    context = "\n".join(lines)

    logger.info(
        "context preview:\n%s",
        context[:2000],
    )

    return context
