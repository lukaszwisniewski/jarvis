"""Agent mode: Claude chooses which sources to search, then we rank and build context.

The graph has three nodes:

    decide ──(tool calls)──> run_tools ──(rounds left)──> decide
       └──(no tool calls)──> finish <──(round limit hit)──┘

`decide` asks Claude what to search, `run_tools` runs the searches, and `finish`
ranks everything with the same rank_results() and build_context() as the fixed
pipeline. The answer itself is written later by ask_llm() in the Celery task,
exactly as in the default path.
"""

import asyncio
import logging
import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.agent.tools import TOOLS, run_tool
from app.config import settings
from app.llm.client import _get_client
from app.llm.prompts import AGENT_SYSTEM_PROMPT
from app.observability.metrics import LLM_REQUEST_DURATION, record_llm_usage
from app.retrieval.context_builder import build_context
from app.retrieval.models import RetrievalResult
from app.retrieval.pipeline import retrieve_context
from app.retrieval.retrieval_service import rank_results

logger = logging.getLogger(__name__)

_EARLY_STOPS = {"max_tokens", "refusal"}

# At most this many searches run per round (one per source). Extra tool calls
# get an error result, because every tool_use block needs a tool_result.
MAX_TOOL_CALLS_PER_ROUND = 3


class AgentState(TypedDict):
    question: str
    scope: str | None
    messages: list  # Claude conversation: user, assistant, tool results, ...
    results: list[RetrievalResult]  # everything the tools have found so far
    tool_rounds: int
    context: str


def _tool_calls(message: dict) -> list:
    return [block for block in message["content"] if block.type == "tool_use"]


def _result_key(result: RetrievalResult) -> tuple[str, str]:
    return (result.source, result.url or result.title)


def build_agent_graph(client, model: str, max_tool_rounds: int):
    """Build the graph. The LLM client is passed in so tests can use a fake."""

    async def decide(state: AgentState) -> dict:
        # The SDK client is synchronous; run it in a thread so the event loop
        # stays free, like generate_queries() in the fixed pipeline.
        start = time.perf_counter()
        outcome = "error"
        try:
            response = await asyncio.to_thread(
                client.messages.create,
                model=model,
                max_tokens=1024,
                system=AGENT_SYSTEM_PROMPT,
                tools=TOOLS,
                messages=state["messages"],
            )
            outcome = "success"
        finally:
            LLM_REQUEST_DURATION.labels("agent_decide", outcome).observe(
                time.perf_counter() - start
            )
        record_llm_usage("agent_decide", response)
        assistant = {"role": "assistant", "content": response.content}

        # stop_reason explains odd runs: "max_tokens" can cut a tool call short
        # and "refusal" ends the search early. Both end in finish, not a crash.
        stop_reason = getattr(response, "stop_reason", None)
        level = logging.WARNING if stop_reason in _EARLY_STOPS else logging.INFO
        logger.log(
            level,
            "agent decide: stop_reason=%s tool_calls=%s",
            stop_reason,
            len(_tool_calls(assistant)),
        )
        return {"messages": state["messages"] + [assistant]}

    def route_after_decide(state: AgentState) -> str:
        if _tool_calls(state["messages"][-1]):
            return "run_tools"
        return "finish"

    def route_after_tools(state: AgentState) -> str:
        # The round limit is the cost guard for Claude calls: after the last
        # allowed round we go straight to finish, because another decide call
        # could not change the result.
        if state["tool_rounds"] >= max_tool_rounds:
            return "finish"
        return "decide"

    async def run_tools(state: AgentState) -> dict:
        calls = _tool_calls(state["messages"][-1])
        allowed = calls[:MAX_TOOL_CALLS_PER_ROUND]
        outcomes = await asyncio.gather(
            *(run_tool(call.name, call.input, state["scope"]) for call in allowed)
        )
        rejected = (
            f"Too many tool calls this round; at most {MAX_TOOL_CALLS_PER_ROUND} "
            "are run per round.",
            [],
            True,
        )
        outcomes += [rejected] * (len(calls) - len(allowed))

        tool_results = []
        found = list(state["results"])
        seen = {_result_key(r) for r in found}
        for call, (text, results, is_error) in zip(calls, outcomes):
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": text,
                    "is_error": is_error,
                }
            )
            for result in results:
                if _result_key(result) not in seen:
                    seen.add(_result_key(result))
                    found.append(result)

        # All results of one round go back in a single user message.
        reply = {"role": "user", "content": tool_results}
        return {
            "messages": state["messages"] + [reply],
            "results": found,
            "tool_rounds": state["tool_rounds"] + 1,
        }

    def finish(state: AgentState) -> dict:
        by_source: dict[str, list[RetrievalResult]] = {}
        for result in state["results"]:
            by_source.setdefault(result.source, []).append(result)
        return {"context": build_context(rank_results(by_source))}

    graph = StateGraph(AgentState)
    graph.add_node("decide", decide)
    graph.add_node("run_tools", run_tools)
    graph.add_node("finish", finish)
    graph.add_edge(START, "decide")
    graph.add_conditional_edges("decide", route_after_decide, ["run_tools", "finish"])
    graph.add_conditional_edges("run_tools", route_after_tools, ["decide", "finish"])
    graph.add_edge("finish", END)
    return graph.compile()


def recursion_limit(max_tool_rounds: int) -> int:
    """LangGraph step limit for a run with at most max_tool_rounds rounds.

    The longest run (the round limit is reached) needs 2 * rounds + 2 steps as
    LangGraph counts them; measured as 18 for 8 rounds. Two spare steps are
    added so a normal run can never hit the limit.
    """
    return 2 * max_tool_rounds + 4


async def agent_retrieve_context(
    question: str,
    scope: str | None = None,
    client=None,
) -> str:
    """Agent-mode replacement for retrieve_context(); returns a context string."""
    if client is None:
        if settings.llm_stub_mode:
            # Stub mode has no real Claude to choose tools, so use the fixed
            # pipeline to keep local development working.
            logger.info("agent mode in LLM stub mode: using the fixed pipeline")
            return await retrieve_context(question, scope=scope)
        client = _get_client()

    graph = build_agent_graph(
        client=client,
        model=settings.anthropic_model_fast,
        max_tool_rounds=settings.agent_max_tool_rounds,
    )
    state = await graph.ainvoke(
        {
            "question": question,
            "scope": scope,
            "messages": [{"role": "user", "content": question}],
            "results": [],
            "tool_rounds": 0,
            "context": "",
        },
        # Set explicitly so the graph does not depend on library defaults or
        # the LANGGRAPH_DEFAULT_RECURSION_LIMIT environment variable.
        config={"recursion_limit": recursion_limit(settings.agent_max_tool_rounds)},
    )
    logger.info(
        "agent finished: tool_rounds=%s results=%s",
        state["tool_rounds"],
        len(state["results"]),
    )
    return state["context"]
