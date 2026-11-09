# JARVIS architecture

JARVIS — Joint Archival Retrieval of Verbose Information Systems.

This document describes the current implementation of JARVIS as it exists in this repository. It reflects the actual runtime flow in the code rather than any historical design notes or future roadmap items.

## Overview

JARVIS is a Slack-first engineering assistant that answers technical questions by collecting evidence from GitHub, Jira, and Confluence, then synthesizing a response with Anthropic. The system is split across a FastAPI HTTP entry point, a Celery worker, provider integrations, and retrieval orchestration.

At a high level:

- Slack receives a slash command such as `/jarvis <question>`
- the FastAPI app validates the request and enqueues a background task
- the Celery worker runs the retrieval and synthesis pipeline
- the app retrieves candidate evidence from multiple sources
- the results are normalized, ranked, and assembled into one context block
- Anthropic synthesizes a final answer from the retrieved context; answers are not independently verified against the sources and do not include per-claim citations
- the response is posted back to Slack using the original `response_url`

---

## Runtime flow

```mermaid
flowchart LR
    U[Slack user] --> S[/jarvis command]
    S --> API[FastAPI app\napp/api/webhooks/slack.py]
    API --> V[Slack signature validation]
    V --> Q[Queue Celery task\nprocess_jarvis_query]
    Q --> W[Celery worker\napp/workers/tasks.py]
    W --> RC[retrieve_context()\napp/retrieval/pipeline.py]
    RC --> GQ[generate_queries()\napp/retrieval/query_generator.py]
    GQ --> RETRIEVE[retrieve()\napp/retrieval/retrieval_service.py]
    RETRIEVE --> GH[GitHub provider]
    RETRIEVE --> J[Jira provider]
    RETRIEVE --> C[Confluence provider]
    GH --> N[Normalize and rank results]
    J --> N
    C --> N
    N --> CTX[build_context()]
    CTX --> LLM[Anthropic synthesis\napp/llm/client.py]
    LLM --> FB[Format Slack response]
    FB --> POST[POST response_url]
    POST --> U
```

## Entry points

### FastAPI app

The application entry point is defined in [app/main.py](../app/main.py). It creates the FastAPI app and includes the Slack router.

Key behaviors:

- app exposes `/health` for basic liveness checks
- the app does not implement a large REST API; the main live path is Slack command handling
- the HTTP layer is responsible for validation and task submission rather than doing retrieval work directly

### Slack command endpoint

The command handling logic lives in [app/api/webhooks/slack.py](../app/api/webhooks/slack.py).

Responsibilities:

- validate the request signature with Slack verification
- parse optional scope prefixes such as `cf` / `coldfusion` and `py` / `python`
- reject empty questions and malformed `response_url` values
- enforce the configured Slack user allowlist when present
- enqueue the asynchronous worker task
- return an ephemeral acknowledgement to the Slack user within the 3-second response window

The route is:

- `POST /slack/commands`

---

## Background worker

The asynchronous worker is implemented in [app/workers/tasks.py](../app/workers/tasks.py) as the Celery task `process_jarvis_query()`.

This function:

- runs the async retrieval pipeline in a synchronous Celery context
- attempts endpoint-inventory response generation first
- falls back to the general Anthropic synthesis path if no endpoint-oriented answer is produced
- adds guardrails for current-state questions
- appends source coverage metadata
- formats the final response for Slack
- posts the payload to the Slack `response_url`

If the Slack post fails with a retryable network or HTTP-server problem, the task retries with a delay.

---

## Retrieval pipeline

The retrieval orchestration lives in [app/retrieval/pipeline.py](../app/retrieval/pipeline.py) and [app/retrieval/retrieval_service.py](../app/retrieval/retrieval_service.py).

### Query generation

The query generator in [app/retrieval/query_generator.py](../app/retrieval/query_generator.py) does two things:

1. requests a small set of code-oriented search queries from the LLM using `ask_llm(..., fast=True)`
2. falls back to deterministic heuristics when the model output is invalid or the query appears to be an endpoint/architecture/table-style question

The fallback logic looks at:

- endpoint terms such as `endpoint`, `route`, and `api`
- architecture terms such as `flow`, `process`, `architecture`, and `worker`
- table/schema terms such as `table`, `database`, and `sql`

### Retrieval execution

The central retrieval function `retrieve()` runs all configured provider searches concurrently with `asyncio.gather(...)`.

The providers currently wired into the system are:

- [app/retrieval/providers/github_provider.py](../app/retrieval/providers/github_provider.py)
- [app/retrieval/providers/jira_provider.py](../app/retrieval/providers/jira_provider.py)
- [app/retrieval/providers/confluence_provider.py](../app/retrieval/providers/confluence_provider.py)

Behavior details:

- provider exceptions are logged and ignored unless the exception is `asyncio.CancelledError`
- successful results from each provider are normalized to the same score scale
- GitHub results receive a small score bias so they rank slightly higher for code-centric questions
- results are capped by provider-specific limits from configuration
- the final list is sorted and truncated to the top ten results

### Context assembly

The context builder in [app/retrieval/context_builder.py](../app/retrieval/context_builder.py) transforms retrieval objects into a plain-text context block that is readable by the LLM.

Each result is rendered with:

- source tag like `[GITHUB]`, `[JIRA]`, or `[CONFLUENCE]`
- title
- snippet
- URL when available
- relevance score

---

## LLM layer

The LLM integration is in [app/llm/client.py](../app/llm/client.py).

Key points:

- it uses the Anthropic Python SDK
- it reads settings from [app/config.py](../app/config.py)
- it supports `llm_stub_mode` for local/test work without real API calls
- it uses a fast model for query generation and a synthesis model for the final answer
- if context is available, it places the retrieved context in the user message, wrapped in `<retrieved_context>` delimiters, before the question; the system prompt itself never contains retrieved content

The repository also includes prompt handling in [app/llm/prompts.py](../app/llm/prompts.py) and endpoint inventory logic in [app/llm/endpoint_inventory.py](../app/llm/endpoint_inventory.py).

### Untrusted retrieved content

Content retrieved from GitHub, Jira and Confluence is treated as untrusted input. It may contain misleading or malicious instructions (prompt injection). JARVIS mitigates this at the prompt level:

- the system prompt states that retrieved content is untrusted reference material, that instructions inside it must be treated as data and not followed, and that it cannot override the system instructions or change the assistant's role or task
- retrieved context is wrapped in `<retrieved_context>` delimiters in the user message; any delimiter tags appearing inside the retrieved content are escaped so the content cannot close the block early

These measures reduce the risk but do not guarantee protection against prompt injection. The LLM has no direct access to tools or the ability to perform actions in external systems. The application posts its generated response to Slack, so a successful injection could still influence the content delivered to users.

---

## Data and configuration model

The global runtime configuration is defined in [app/config.py](../app/config.py).

The settings model includes:

- Slack credentials and user allowlist
- Anthropic API configuration and model selection
- GitHub repo and extension settings
- Jira and Confluence connection settings
- Redis URL and general runtime config
- retrieval tuning values such as source caps and keyword lists

The settings system uses Pydantic `BaseSettings` and loads values from environment variables and a local `.env` file. Several config fields are normalized from comma-separated strings or JSON arrays into list/dict structures.

Examples of tunables include:

- `retrieval_source_caps`
- `retrieval_endpoint_intent_keywords`
- `retrieval_architecture_intent_keywords`
- `retrieval_table_intent_keywords`
- `retrieval_query_stopwords`

---

## Provider and integration boundaries

### GitHub

GitHub retrieval is governed by provider-specific repo and extension settings, with optional profile-based overrides for `cf` and `py` scopes. The provider and related connector logic are under:

- [app/connectors/github.py](../app/connectors/github.py)
- [app/retrieval/providers/github_provider.py](../app/retrieval/providers/github_provider.py)

### Jira

Jira access is configured via token, base URL, and email settings; the retrieval path uses the Jira connector plus the Jira provider implementation.

Relevant files:

- [app/connectors/jira.py](../app/connectors/jira.py)
- [app/retrieval/providers/jira_provider.py](../app/retrieval/providers/jira_provider.py)

### Confluence

Confluence retrieval uses the base URL configured in settings and a dedicated provider implementation.

Relevant files:

- [app/connectors/confluence.py](../app/connectors/confluence.py)
- [app/retrieval/providers/confluence_provider.py](../app/retrieval/providers/confluence_provider.py)

### Slack

The Slack integration handles request verification and formatting:

- [app/slack/client.py](../app/slack/client.py): signature validation
- [app/slack/formatting.py](../app/slack/formatting.py): response formatting

---

## Current architectural constraints

This repo is best understood as a Slack-based engineering assistant prototype rather than as a generalized, production-grade service.

Notable constraints visible in the code:

- the app is Slack-centric, not a generic multi-channel application
- retrieval depends on live external APIs and credentials
- results are stateless; there is no persisted conversation memory
- the worker relies on Redis and Celery for asynchronous execution
- the retrieval and ranking model is heuristic-based rather than a learned semantic index

---

## Operational notes

The local runtime is configured via Docker Compose:

- API service runs uvicorn on port 8000
- worker service runs Celery
- Redis service runs on port 6379

The repository also includes a basic health endpoint used for liveness checks.

This architecture is intentionally narrow and implementation-driven: the code is the source of truth for what JARVIS actually does today.
