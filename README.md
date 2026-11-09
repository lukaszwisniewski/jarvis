# JARVIS — Joint Archival Retrieval of Verbose Information Systems

![CI](https://github.com/lukaszwisniewski/jarvis/actions/workflows/ci.yml/badge.svg)

JARVIS is a retrieval-first engineering assistant that answers technical questions using evidence gathered from software development tools. It retrieves relevant context from GitHub, Jira, and Confluence before asking an LLM to synthesize an answer, and delivers the result through a Slack slash command.

The project is built with **Python, FastAPI, Celery, Redis, and the Anthropic API**. It is a working prototype/portfolio project, not a turnkey hosted service.

## What it does

A user submits a question in Slack:

```text
/jarvis how does the authentication flow work?
```

JARVIS then:

1. verifies the incoming Slack request;
2. parses the question and optional retrieval scope;
3. generates focused search queries;
4. retrieves relevant material from GitHub, Jira, and Confluence;
5. normalizes scores per source, removes duplicates within each source, applies per-source limits and ranks the results heuristically;
6. formats the top results (up to ten) into a plain-text context with source labels and URLs;
7. uses Anthropic to synthesize an answer from that context;
8. posts the answer back to Slack with a footer showing how many results came from each source.

The application also exposes `GET /health` for a basic health check.

## Architecture

```mermaid
flowchart LR
    A[Slack slash command] --> B[FastAPI webhook]
    B --> C[Signature verification]
    C --> D[Celery task]
    D --> E[Retrieval pipeline]
    E --> F[Query generation]
    F --> G[GitHub / Jira / Confluence]
    G --> H[Normalize, deduplicate and rank]
    H --> I[Context builder]
    I --> J[Anthropic synthesis]
    J --> K[Slack response]
    D <--> R[(Redis)]
```

### Main components

| Component | Responsibility |
|---|---|
| FastAPI | HTTP API and Slack webhook |
| Slack integration | Request signature verification and response formatting |
| Celery | Asynchronous execution of retrieval and answer generation |
| Redis | Celery broker and result backend |
| Retrieval pipeline | Coordinates query generation and source retrieval |
| GitHub, Jira, Confluence providers | Fetch and normalize source-specific results |
| Context builder | Formats the top-ranked results into a plain-text context |
| Anthropic client | Generates the final answer from the question and retrieved context |

The implementation is deliberately retrieval-first: the LLM is given source material gathered for the question rather than being asked to answer solely from its general knowledge. The system prompt instructs the model to base implementation details on that material, but answers are not checked against it and do not include per-claim citations.

## Technical highlights

- FastAPI application with a separate Celery worker.
- Asynchronous retrieval across multiple external providers.
- Provider-specific connectors with HTTP timeout and retry handling.
- Query generation and intent-aware retrieval.
- Per-source score normalization, within-source deduplication, heuristic ranking, and per-source limits.
- Context construction with source labels and URLs, plus a per-source result-count footer in each answer.
- Slack request-signature verification.
- Retrieved content treated as untrusted input: delimited in the prompt, with system-prompt instructions not to follow instructions found inside it.
- Anthropic API integration for context-based answer synthesis.
- Unit and integration tests using pytest, mocks, and fixtures.
- Optional LangGraph agent mode in which Claude chooses which sources to search through tools, with a limit on tool rounds.
- Offline retrieval evaluation (recall@k, MRR per source) over a fixed synthetic corpus, run in CI as a regression gate.
- Docker Compose setup for local API, worker, and Redis services.

## Project structure

```text
app/
  api/                 FastAPI routes and Slack webhook
  connectors/          GitHub, Jira, Confluence and HTTP utilities
  agent/               Optional LangGraph agent: search tools and graph
  llm/                 Anthropic client, prompts and deterministic helpers
  retrieval/           Query generation, providers, ranking and context building
  slack/               Slack verification and response formatting
  workers/             Celery application and background tasks
tests/
  unit/
  integration/
eval/                  Offline retrieval evaluation: corpus, golden set, metrics, runner
docs/
  architecture.md
  development.md
```

## Requirements

- Python 3.12
- Poetry
- Docker and Docker Compose
- Credentials for the integrations you want to use:
  - Slack app signing secret
  - Anthropic API key
  - GitHub token
  - Jira token, base URL and account email
  - Confluence base URL (Confluence requests reuse the Jira email and token)

The application can be configured for the providers available to your team. GitHub repository lists and retrieval scopes are supplied through environment variables; no organization-specific repository defaults are included.

## Local setup

Clone the repository and install dependencies:

```bash
poetry install --with dev
```

Create a local environment file from the template (Docker Compose requires `.env` to exist):

```bash
cp .env.example .env
```

Replace the placeholder values in `.env`. At minimum, a live Slack request requires `SLACK_SIGNING_SECRET`, and live answer synthesis requires `ANTHROPIC_API_KEY` (or set `LLM_STUB_MODE=true`). Provider credentials are needed only for the integrations you use.

Start the local stack:

```bash
docker compose up --build
```

The Compose stack starts:

- the FastAPI application on port `8000`;
- the Celery worker;
- Redis on port `6379`.

Check the API:

```bash
curl http://localhost:8000/health
```

Expected response:

```json
{"status": "ok"}
```

## Configuration

All settings are defined in [`app/config.py`](app/config.py) and can be supplied through environment variables or the local `.env` file.

The main settings include:

| Variable | Purpose |
|---|---|
| `SLACK_SIGNING_SECRET` | Verifies incoming Slack requests |
| `SLACK_ALLOWED_USER_IDS` | Optional comma-separated allowlist of Slack user IDs; empty allows all users |
| `ANTHROPIC_API_KEY` | Enables live answer synthesis |
| `ANTHROPIC_MODEL_FAST`, `ANTHROPIC_MODEL_SYNTHESIS` | Optional model overrides for query generation and answer synthesis |
| `LLM_STUB_MODE` | Enables stubbed LLM responses for tests/development |
| `AGENT_MODE`, `AGENT_MAX_TOOL_ROUNDS` | Optional agent mode (off by default) and its limit on rounds of tool calls (default 4, allowed 1–8); see [Agent mode](#agent-mode-optional) |
| `GITHUB_TOKEN` | Authenticates GitHub retrieval |
| `GITHUB_REPOS` | Default GitHub repositories to search; if empty, code search is not restricted to any repository |
| `GITHUB_CF_REPOS`, `GITHUB_PY_REPOS` | Optional repository lists for the `cf` / `py` scope prefixes |
| `GITHUB_*_ALLOWED_EXTENSIONS`, `GITHUB_*_EXTENSION_SCORES` | Optional file-extension filters and ranking weights (default, `CF` and `PY` profiles) |
| `JIRA_TOKEN`, `JIRA_BASE_URL`, `JIRA_EMAIL` | Jira connection settings (Atlassian Cloud REST API v3) |
| `CONFLUENCE_BASE_URL` | Confluence site root, without `/wiki`; authenticates with the Jira email and token; empty disables Confluence |
| `REDIS_URL` | Celery broker/backend URL |
| `RETRIEVAL_SOURCE_CAPS` | Maximum number of results retained per source |
| `RETRIEVAL_*_KEYWORDS` | Terms used by retrieval intent and query preprocessing |

List and mapping values must be written as JSON, for example `GITHUB_REPOS=["your-org/your-repo"]` or `RETRIEVAL_SOURCE_CAPS={"github": 6, "jira": 3, "confluence": 1}`. Comma-separated values are not parsed by the settings loader.

See [`.env.example`](.env.example) for all supported keys, defaults and formats. Keep real credentials in `.env` or your secret manager; never commit them.

## Tests and code quality

Run the test suite:

```bash
poetry run pytest -q
```

Run Ruff:

```bash
poetry run ruff check .
```

Ruff and the test suite run in GitHub Actions on every push and pull request (see `.github/workflows/ci.yml`). Run the same checks locally with the commands above.

## Agent mode (optional)

By default JARVIS runs a fixed pipeline. It generates search queries, queries GitHub, Jira and Confluence at the same time, ranks the results and synthesises an answer. With `AGENT_MODE=true`, the evidence-gathering step is replaced by a small [LangGraph](https://github.com/langchain-ai/langgraph) agent in `app/agent/`:

```text
decide ──(tool calls)──> run_tools ──(rounds left)──> decide
   └──(no tool calls)──> finish <──(round limit hit)──┘
```

- **decide:** Claude (`ANTHROPIC_MODEL_FAST`) sees the question and three tools: `search_github`, `search_jira` and `search_confluence`. It calls the ones it thinks will help, with short keyword queries.
- **run_tools:** each tool searches one source through the same providers as the fixed pipeline. Tool output is wrapped in `<retrieved_context>` tags, escaped and capped at 4,000 characters. A failing source or an invalid tool call comes back to Claude as an error result instead of stopping the run.
- **Loop limit:** the loop stops when Claude stops calling tools, or after `AGENT_MAX_TOOL_ROUNDS` rounds of tool calls (default 4, allowed 1–8).
- **finish:** everything found is ranked with the same normalisation, GitHub bias and per-source caps as the fixed pipeline, and formatted as the same context block.

After that, both modes do the same thing: answer synthesis through `ask_llm`, the current-state guardrail and the source footer. In `LLM_STUB_MODE`, agent mode falls back to the fixed pipeline, because there is no real model to choose tools.

### Tradeoffs compared with the fixed pipeline

| | Fixed pipeline (default) | Agent mode |
|---|---|---|
| Claude calls per question | 2: query generation and synthesis | One decide call per tool round, one more if Claude stops searching by itself, and synthesis. That is at most `AGENT_MAX_TOOL_ROUNDS` + 1, so 5 with the default limit |
| Source calls | All three sources, every time, in parallel | Only the sources Claude picks. Rounds run one after another, which adds latency |
| Queries per source | Up to 5 generated queries | One query per tool call. Claude can retry with different keywords |
| Repeatability | The search plan is fixed; only the generated queries can vary | Which sources are searched, and how often, can differ between runs |
| Prompt-injection surface | Retrieved text reaches only the synthesis call | Retrieved text also reaches the calls that decide the next search. It is delimited, escaped and size-capped, which reduces this risk but does not remove it |
| Evaluation | Covered by the offline eval | **Not measured.** The offline eval runs in stub mode, where agent mode uses the fixed pipeline |

Agent mode is a demonstration of tool-based retrieval, not a measured improvement. No claim is made that it gives better answers than the fixed pipeline.

## Evaluation

`eval/` contains an offline retrieval evaluation. It runs the real query fallback, connectors, providers and ranking (`retrieve()`) against a fake HTTP backend that serves a small synthetic corpus, and scores the ranked results against hand-labelled relevant documents.

```bash
poetry run python -m eval.run_retrieval_eval
```

It needs no credentials or network access. Unknown URLs fail instead of being requested. A run takes a few seconds and writes per-question details to `eval/results/retrieval.json`.

**What is measured:** the deterministic query fallback (query generation runs in stub mode), JARVIS's local re-scoring, per-source normalisation, GitHub bias, per-source caps and the final top-10 ranking.

**What is not measured:** the quality of GitHub, Jira or Confluence search (the fake backend selects candidates with simple word overlap), LLM-generated queries, behaviour on real data, or answer quality.

### Results

Current numbers from `eval/results/retrieval.json`: 20 questions over 19 GitHub files, 12 Jira issues and 8 Confluence pages.

| Source | Questions | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|
| Overall | 20 | 0.83 | 0.95 | 0.76 |
| GitHub | 20 | 0.78 | 0.97 | 0.74 |
| Jira | 12 | 1.00 | 1.00 | 0.42 |
| Confluence | 11 | 0.73 | 0.82 | 0.25 |

Per-source rows count only that source's relevant documents, but use their positions in the final merged ranking. The low Jira and Confluence MRR reflects the deliberate GitHub bias and the default cap of one Confluence result. The misses come from the query fallback. It keeps question words such as "why" and "which", and for database questions it can pick "which" as the entity to search for.

Caveats: the corpus and labels were written by the project author and are small. The numbers detect regressions in this code. They are not a benchmark of retrieval quality on real repositories. Settings in a local `.env` (caps, keywords, extensions) change the results; the committed numbers use the defaults. After the first run, the fake backend was changed to split identifiers such as `payment_service` into separate words, as real search engines do; this raised overall recall@10 from 0.89 to 0.95.

CI runs the eval and fails if overall recall@10 drops below 0.90. That gate was set just below the first measured value of 0.95.

### Optional: LLM-as-judge groundedness check

`eval/judge.py` retrieves context the same way, generates an answer through the normal synthesis path, and asks Claude whether every claim in the answer is supported by the context. Both the context and the answer are passed to the judge as delimited, untrusted data. It makes paid API calls, so it is off unless you opt in:

```bash
EVAL_LLM_JUDGE=1 ANTHROPIC_API_KEY=... poetry run python -m eval.judge
```

The output goes to `eval/results/judge.json`, which is gitignored. No judge results are published in this repository, and a judge model can itself be wrong.

## Current scope and limitations

- Slack is the implemented user interface; this is not currently a general-purpose web chat application.
- Retrieval depends on access to the configured external APIs and their permissions.
- Answer quality depends on the relevance and completeness of retrieved source material. Retrieval ranking is evaluated offline against a small synthetic corpus (see [Evaluation](#evaluation)); there is no evaluation against real GitHub, Jira or Confluence data, and no published measurement of answer quality.
- The retrieval and ranking stages use application logic and heuristics; there is no vector database or semantic code index in the current implementation.
- The project does not include a hosted production deployment or infrastructure provisioning.
- Conversation history is not persisted as long-term assistant memory.
- Agent mode is optional, off by default and not covered by the offline evaluation.
- Prompt-injection handling is a prompt-level mitigation (untrusted-content instructions and delimiters), not complete protection.

JARVIS is intended to demonstrate practical backend engineering: API integration, asynchronous processing, retrieval orchestration, failure handling, testing, and LLM integration.
