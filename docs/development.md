# JARVIS development guide

This is the practical setup guide for the current JARVIS implementation in this repository. For the system overview, see [README.md](../README.md). For the runtime architecture and component flow, see [architecture.md](./architecture.md).

This guide intentionally reflects the code in this repo and not historical design material or future deployment plans.

---

## 1. Prerequisites

The current project configuration in [pyproject.toml](../pyproject.toml) declares:

- Python `>=3.12,<4.0`
- Poetry for dependency management
- Docker Compose for running the API, worker, and Redis services

The repo also expects the following local tools to be available:

- Python 3.12
- Poetry
- Docker Desktop or Docker Engine with Compose support

The app is implemented as a FastAPI service plus a Celery worker, and [docker-compose.yml](../docker-compose.yml) defines the local runtime stack:

- `api` service
- `worker` service
- `redis` service

---

## 2. Environment configuration

### How settings are loaded

The runtime configuration is defined in [app/config.py](../app/config.py). The application uses Pydantic `BaseSettings` and loads values from:

- environment variables
- a local `.env` file via `SettingsConfigDict(env_file=".env")`

The `Settings` object is created at the bottom of the file:

```python
settings = Settings()
```

The configuration also contains validators that normalize several list and dictionary fields, such as:

- repo lists
- extension lists
- extension score maps
- source caps
- retrieval keyword lists

Pydantic Settings decodes list and dictionary values from environment variables and `.env` as JSON before these validators run, so these values must be written as JSON, for example `GITHUB_REPOS=["your-org/your-repo"]`. Comma-separated values currently fail to parse.

### What the code treats as required vs optional

The repository includes a `.env.example` template. Copy it to `.env` and populate the credentials and provider settings needed for your setup. The source of truth for supported settings is the model in [app/config.py](../app/config.py).

The settings that are clearly required for the main runtime path include:

- `slack_signing_secret`: required to validate incoming Slack requests
- `anthropic_api_key`: required for live LLM synthesis unless `llm_stub_mode` is enabled

Settings that are required only when a particular integration path is used include:

- `github_token`
- `jira_token`
- `jira_base_url`
- `jira_email`
- `confluence_base_url`

These are part of the provider configuration but are not all required for the app to boot. They matter when the corresponding provider is used. Confluence requests authenticate with `jira_email` and `jira_token`, and `confluence_base_url` should be the site root without `/wiki`. If `github_repos` is empty, GitHub code search is not restricted to any repository.

Settings that are optional or primarily development-oriented include:

- `slack_allowed_user_ids`
- `llm_stub_mode`
- `anthropic_model_fast`, `anthropic_model_synthesis` (model overrides)
- `redis_url` (has default value `redis://localhost:6379/0`)

`slack_bot_token` and `log_level` are defined in `app/config.py` but are not currently used by the code, so they are omitted from `.env.example`.

The repo also contains several configurable retrieval settings with built-in defaults, for example:

- `retrieval_source_caps`
- `retrieval_endpoint_intent_keywords`
- `retrieval_architecture_intent_keywords`
- `retrieval_table_intent_keywords`
- `retrieval_query_stopwords`
- `retrieval_table_entity_exclude_keywords`

The GitHub-specific profile settings are also defined in settings, including:

- `github_repos`
- `github_allowed_extensions`
- `github_extension_scores`
- `github_cf_repos`
- `github_cf_allowed_extensions`
- `github_cf_extension_scores`
- `github_py_repos`
- `github_py_allowed_extensions`
- `github_py_extension_scores`

### Practical rule for a local setup

Before starting the app, make sure your local environment has the values expected by the settings model in [app/config.py](../app/config.py). Use a `.env` file in the project root if you are running the stack with Docker Compose, because the Compose file declares:

```yaml
env_file: .env
```

Use the checked-in `.env.example` as a starting point, and refer to `app/config.py` for defaults and validation behaviour.

---

## 3. Starting the local runtime

From the repository root:

```bash
poetry install --with dev
cp .env.example .env
```

Replace the placeholder values in `.env`, then start the existing local services with Docker Compose:

```bash
docker compose up --build
```

This is the runtime defined in [docker-compose.yml](../docker-compose.yml).

The stack starts:

- an `api` service running:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

- a `worker` service running:

```bash
celery -A app.workers.celery_app:celery_app worker --loglevel=info
```

- a Redis container on port `6379`

The Compose file also sets the following environment overrides for API and worker:

```yaml
REDIS_URL=redis://redis:6379/0
NO_PROXY=localhost,127.0.0.1,redis
no_proxy=localhost,127.0.0.1,redis
```

This is a repository-supported workaround for the IPv6/NO_PROXY issue noted in the config comments.

---

## 4. Checking application health

The app health endpoint is defined in [app/main.py](../app/main.py):

```python
@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
```

Check it with:

```bash
curl http://localhost:8000/health
```

Expected response:

```json
{"status": "ok"}
```

---

## 5. Running the test suite

The project declares pytest in [pyproject.toml](../pyproject.toml). The current repo validation command is:

```bash
poetry run pytest -q
```

This is the command used in the current repository workflow.

---

## 6. Troubleshooting

### Missing or invalid Slack signature

The Slack webhook route in [app/api/webhooks/slack.py](../app/api/webhooks/slack.py) validates the request using the Slack signing secret. If the request fails verification, the endpoint returns a `403`.

Common causes:

- missing `slack_signing_secret`
- manually testing the route without a valid Slack request signature
- a malformed Slack request body

### LLM calls fail or API is not configured

The LLM client in [app/llm/client.py](../app/llm/client.py) raises an error when it is asked to call Anthropic without an API key unless `llm_stub_mode` is enabled.

Check:

- `anthropic_api_key`
- `llm_stub_mode`

If `llm_stub_mode` is enabled, the app returns a stub payload instead of a live Anthropic call.

### Redis connection issues

The worker and API both expect Redis to be available and use the Compose-provided URL:

```text
redis://redis:6379/0
```

If running outside the stack, the code default is:

```text
redis://localhost:6379/0
```

If the worker cannot connect, confirm the expected port and that the Redis container is running.

### Docker/host networking issue

The Compose file explicitly sets `NO_PROXY` and `no_proxy` to avoid the InvalidURL issue noted for some local host networking setups. This is already in the repository configuration and should be left as-is unless you are intentionally overriding it in a custom local setup.

### Missing `.env` or environment values

The app loads settings from `.env`, and Docker Compose requires the file to exist. Create it with `cp .env.example .env`. If the app fails at startup with a `SettingsError`, check that list and dictionary values are valid JSON. Otherwise, compare your environment values against [app/config.py](../app/config.py).

---

## 7. Development workflow summary

The repository’s current local workflow is straightforward:

1. install dependencies with Poetry
2. copy `.env.example` to `.env` and populate the values required by [app/config.py](../app/config.py)
3. start the stack with Docker Compose
4. check `/health`
5. run `poetry run pytest -q` as the project validation command

This is the practical workflow for the present implementation, and it is intentionally limited to the code that exists in this repo.
