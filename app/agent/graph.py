"""Agent mode: Claude chooses which sources to search, then we rank and build context.

The graph has three nodes:

    decide ──(tool calls and rounds left)──> run_tools ──> decide
       └────(no tool calls, or limit hit)──> finish ──> END

`decide` asks Claude what to search, `run_tools` runs the searches, and `finish`
ranks everything with the same rank_results() and build_context() as the fixed
pipeline. The answer itself is written later by ask_llm() in the Celery task,
exactly as in the default path.
"""

import asyncio
import logging
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.agent.tools import TOOLS, run_tool
from app.config import settings
from app.llm.client import _get_client
from app.llm.prompts import AGENT_SYSTEM_PROMPT
from app.retrieval.context_builder import build_context
from app.retrieval.models import RetrievalResult
from app.retrieval.pipeline import retrieve_context
from app.retrieval.retrieval_service import rank_results

logger = logging.getLogger(__name__)


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
        response = await asyncio.to_thread(
            client.messages.create,
            model=model,
            max_tokens=1024,
            system=AGENT_SYSTEM_PROMPT,
            tools=TOOLS,
            messages=state["messages"],
        )
        assistant = {"role": "assistant", "content": response.content}
        return {"messages": state["messages"] + [assistant]}

    def route_after_decide(state: AgentState) -> str:
        wants_tools = bool(_tool_calls(state["messages"][-1]))
        # The round limit is the cost guard: at most max_tool_rounds rounds of
        # searches, however many times Claude asks for more.
        if wants_tools and state["tool_rounds"] < max_tool_rounds:
            return "run_tools"
        return "finish"

    async def run_tools(state: AgentState) -> dict:
        calls = _tool_calls(state["messages"][-1])
        outcomes = await asyncio.gather(
            *(run_tool(call.name, call.input, state["scope"]) for call in calls)
        )

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
    graph.add_edge("run_tools", "decide")
    graph.add_edge("finish", END)
    return graph.compile()


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
        max_tool_rounds=settings.agent_max_steps,
    )
    state = await graph.ainvoke(
        {
            "question": question,
            "scope": scope,
            "messages": [{"role": "user", "content": question}],
            "results": [],
            "tool_rounds": 0,
            "context": "",
        }
    )
    logger.info(
        "agent finished: tool_rounds=%s results=%s",
        state["tool_rounds"],
        len(state["results"]),
    )
    return state["context"]
