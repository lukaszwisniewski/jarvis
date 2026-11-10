"""Search tools the agent can call: one tool per retrieval source."""

import logging

from app.llm.client import (
    RETRIEVED_CONTEXT_CLOSE,
    RETRIEVED_CONTEXT_OPEN,
    _escape_delimiters,
)
from app.retrieval.context_builder import build_context
from app.retrieval.models import RetrievalResult
from app.retrieval.retrieval_service import retrieve_from_source

logger = logging.getLogger(__name__)

# Tool name -> retrieval source used by retrieve_from_source().
TOOL_SOURCES = {
    "search_github": "github",
    "search_jira": "jira",
    "search_confluence": "confluence",
}
MAX_QUERY_LENGTH = 200
MAX_TOOL_OUTPUT_CHARS = 4000

_DESCRIPTIONS = {
    "search_github": "Search source code, configuration and READMEs in GitHub.",
    "search_jira": "Search Jira tickets for bugs, history and decisions.",
    "search_confluence": "Search Confluence pages for designs, runbooks, policies.",
}

TOOLS = [
    {
        "name": name,
        "description": _DESCRIPTIONS[name],
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A few search keywords, not a full sentence.",
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    }
    for name in TOOL_SOURCES
]


def format_tool_output(results: list[RetrievalResult]) -> str:
    """Render results for Claude as delimited, escaped, size-limited text."""
    if not results:
        return "No results."
    # Tool output is untrusted, exactly like the context in the normal
    # pipeline: same tags, same escaping, plus a size cap per tool call.
    text = _escape_delimiters(build_context(results)[:MAX_TOOL_OUTPUT_CHARS])
    return f"{RETRIEVED_CONTEXT_OPEN}\n{text}\n{RETRIEVED_CONTEXT_CLOSE}"


async def run_tool(
    name: str,
    tool_input: object,
    scope: str | None = None,
) -> tuple[str, list[RetrievalResult], bool]:
    """Run one tool call. Returns (text for Claude, results, is_error).

    Errors are returned as text instead of raised, so a bad tool call or a
    failing source never crashes the agent; Claude sees the error and moves on.
    """
    source = TOOL_SOURCES.get(name)
    if source is None:
        return f"Unknown tool: {name}", [], True

    query = tool_input.get("query") if isinstance(tool_input, dict) else None
    if not isinstance(query, str) or not query.strip():
        return "Invalid input: 'query' must be a non-empty string.", [], True
    query = query.strip()[:MAX_QUERY_LENGTH]

    try:
        results = await retrieve_from_source(source, [query], scope=scope)
    except Exception as exc:
        logger.warning("agent tool failed: tool=%s error=%s", name, exc)
        return f"{source} search failed.", [], True

    return format_tool_output(results), results, False
