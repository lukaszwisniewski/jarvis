import asyncio
import logging

from app.retrieval.context_builder import build_context
from app.retrieval.query_generator import generate_queries
from app.retrieval.retrieval_service import retrieve

logger = logging.getLogger(__name__)


async def retrieve_context(query: str, scope: str | None = None) -> str:
    """Build a retrieval context string for a user query.

    The function generates candidate queries, runs the provider retrieval layer,
    logs the resulting summary, and returns a formatted context string for the
    downstream synthesis step.

    Args:
        query: Raw question from the user.
        scope: Optional retrieval scope passed through to the provider layer.

    Returns:
        str: The assembled context string built from the retrieved results.
    """
    logger.info("starting retrieval pipeline")

    queries = await asyncio.to_thread(
        generate_queries,
        query,
    )

    retrieval_results = await retrieve(
        queries,
        scope=scope,
    )

    # Queries, titles and snippets are user-derived or retrieved text, so they
    # are only logged at DEBUG; INFO and WARNING lines carry counts only.
    logger.debug(
        "queries generated: %s",
        queries,
    )

    if not retrieval_results:
        logger.warning(
            "retrieval returned no results for %s queries",
            len(queries),
        )
    else:
        logger.info(
            "retrieval returned %s results",
            len(retrieval_results),
        )

        for result in retrieval_results[:10]:
            logger.debug(
                "retrieval result: source=%s score=%s title=%s snippet=%s",
                getattr(result, "source", "unknown"),
                getattr(result, "score", "unknown"),
                getattr(result, "title", "unknown"),
                str(getattr(result, "snippet", ""))[:100],
            )

    context = build_context(retrieval_results)

    logger.info(
        "context built with length=%s",
        len(context),
    )

    return context
