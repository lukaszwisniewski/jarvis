"""Prometheus metrics for JARVIS.

There are two groups:

* API metrics are registered in API_REGISTRY and served by the API on
  /metrics. The API imports the worker code (slack.py imports the task), so
  worker metrics also exist in the API process; they are kept out of the
  API's endpoint by using a separate registry.
* Worker metrics use prometheus_client's default registry. In the worker
  container, PROMETHEUS_MULTIPROC_DIR makes every process write its values
  to files, and the worker's own endpoint (port 9100) adds them up.

Labels are limited to source, outcome, reason and mode, each with a small
fixed set of values. Never use user IDs, queries, URLs, titles or Jira keys
as labels. No gauges are defined, so no multiprocess gauge mode is needed.
"""

from prometheus_client import CollectorRegistry, Counter, Histogram

# Claude calls and whole tasks often take more than the default 10 s bucket.
_SLOW_BUCKETS = (0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120)

# --- API (served by the API on /metrics) ------------------------------------

API_REGISTRY = CollectorRegistry()

API_REQUEST_DURATION = Histogram(
    "jarvis_api_request_duration_seconds",
    "API request latency. outcome is the status class, e.g. 2xx.",
    ["outcome"],
    registry=API_REGISTRY,
)
TASKS_ENQUEUED = Counter(
    "jarvis_api_tasks_enqueued_total",
    "Slash commands handed to Celery. outcome: success or error.",
    ["outcome"],
    registry=API_REGISTRY,
)

# --- Worker (served by the worker on port 9100) ------------------------------

TASK_DURATION = Histogram(
    "jarvis_task_duration_seconds",
    "process_jarvis_query run time. mode: pipeline or agent; "
    "outcome: success, retry or failure.",
    ["mode", "outcome"],
    buckets=_SLOW_BUCKETS,
)
TASK_RETRIES = Counter(
    "jarvis_task_retries_total",
    "Task retries after a failed Slack POST. Each retry reruns the whole "
    "task, including every Claude call. reason: slack_5xx, slack_timeout, "
    "slack_network.",
    ["reason"],
)
SOURCE_REQUEST_DURATION = Histogram(
    "jarvis_source_request_duration_seconds",
    "Time to search one source. source: github, jira, confluence; "
    "outcome: success or error.",
    ["source", "outcome"],
    buckets=_SLOW_BUCKETS,
)
HTTP_RETRIES = Counter(
    "jarvis_http_retries_total",
    "Connector HTTP retries. reason: status_429, status_5xx, timeout, network.",
    ["source", "reason"],
)
HTTP_TIMEOUTS = Counter(
    "jarvis_http_timeouts_total",
    "Connector HTTP timeouts, whether or not a retry followed.",
    ["source"],
)
LLM_REQUEST_DURATION = Histogram(
    "jarvis_llm_request_duration_seconds",
    "Claude call latency. mode: query_generation, synthesis or agent_decide; "
    "outcome: success or error.",
    ["mode", "outcome"],
    buckets=_SLOW_BUCKETS,
)
LLM_INPUT_TOKENS = Counter(
    "jarvis_llm_input_tokens_total",
    "Input tokens reported by the Anthropic API.",
    ["mode"],
)
LLM_OUTPUT_TOKENS = Counter(
    "jarvis_llm_output_tokens_total",
    "Output tokens reported by the Anthropic API.",
    ["mode"],
)


def status_class(status_code: int) -> str:
    return f"{status_code // 100}xx"


def record_llm_usage(mode: str, response) -> None:
    """Add token counts from an Anthropic response, if it reports usage."""
    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    if isinstance(input_tokens, int):
        LLM_INPUT_TOKENS.labels(mode=mode).inc(input_tokens)
    if isinstance(output_tokens, int):
        LLM_OUTPUT_TOKENS.labels(mode=mode).inc(output_tokens)
