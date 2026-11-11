"""process_jarvis_query stores nothing in the Celery result backend.

Failure and retry records would hold the Slack response URL and its token
(in exc_message and the traceback), and nothing reads task results.

Positive control: a test-only task runs the same function with
ignore_result=False. It is a separate task, not a patch, because Celery
caches a task's send options on the first apply_async, so changing
ignore_result on a task afterwards has no effect.
"""

import json
import logging
import uuid
from unittest.mock import patch

import httpx
from prometheus_client import REGISTRY

import app.workers.tasks as tasks
from app.workers.celery_app import celery_app
from tests.integration.conftest import task_finished, wait_until
from tests.log_capture import RecordCollector

TOKEN = "FAKEtok3nC2xyz"
REAL_TASK = "app.workers.tasks.process_jarvis_query"

# Same body as the real task (the function behind it), results stored.
storing_control_task = celery_app.task(
    bind=True,
    max_retries=2,
    ignore_result=False,
    name="tests.integration.test_task_result_storage.storing_control_task",
)(type(celery_app.tasks[REAL_TASK]).run)

FLOWS = {
    "exhausted": [503, 503, 503],  # RETRY, RETRY, FAILURE
    "client_error": [404],  # FAILURE
    "network": [httpx.ConnectError("connection reset"), 200],  # RETRY, SUCCESS
}


def metric(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def run_flows(task, fake_slack) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Run every flow through the worker; return task IDs and backend writes."""
    backend_class = type(celery_app.backend)
    real_set = backend_class.set
    writes: list[tuple[str, str]] = []

    def recording_set(self, key, value, *args, **kwargs):
        writes.append((_text(key), _text(value)))  # the full serialised record
        return real_set(self, key, value, *args, **kwargs)

    root = logging.getLogger()
    collector = RecordCollector()
    root.addHandler(collector)
    task_ids: dict[str, str] = {}
    try:
        with patch.object(backend_class, "set", recording_set):
            for name, outcomes in FLOWS.items():
                url = fake_slack.script(
                    f"https://hooks.slack.com/commands/T000/{name}/{TOKEN}", *outcomes
                )
                task_ids[name] = str(uuid.uuid4())
                task.apply_async(
                    kwargs={
                        "query": "q",
                        "response_url": url,
                        "user_id": "U1",
                        "scope": None,
                    },
                    task_id=task_ids[name],
                )
            for task_id in task_ids.values():
                wait_until(lambda tid=task_id: task_finished(collector.records, tid))
    finally:
        root.removeHandler(collector)
    for name, remaining in fake_slack.scripts.items():
        assert remaining == [], f"{name}: scripted responses not used"
    return task_ids, writes


def _text(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def test_process_jarvis_query_stores_no_results(in_memory_celery, fake_slack) -> None:
    before = {
        "5xx": metric("jarvis_task_retries_total", reason="slack_5xx"),
        "network": metric("jarvis_task_retries_total", reason="slack_network"),
        **{
            outcome: metric(
                "jarvis_task_duration_seconds_count", mode="pipeline", outcome=outcome
            )
            for outcome in ("retry", "failure", "success")
        },
    }

    # The real task object, called the way the webhook calls it.
    task_ids, writes = run_flows(tasks.process_jarvis_query, fake_slack)

    # No SUCCESS, FAILURE or RETRY record was written.
    assert writes == [], [key for key, _ in writes]
    for task_id in task_ids.values():
        assert celery_app.backend.get_task_meta(task_id)["status"] == "PENDING"

    # Retries and outcomes are counted exactly as before.
    assert metric("jarvis_task_retries_total", reason="slack_5xx") == before["5xx"] + 2
    assert (
        metric("jarvis_task_retries_total", reason="slack_network")
        == before["network"] + 1
    )
    for outcome, runs in {"retry": 3, "failure": 2, "success": 1}.items():
        assert (
            metric(
                "jarvis_task_duration_seconds_count", mode="pipeline", outcome=outcome
            )
            == before[outcome] + runs
        )

    # The setting comes from the decorator, as the worker and API load it.
    assert celery_app.tasks[REAL_TASK].ignore_result is True


def test_control_task_with_stored_results_leaks_url(
    in_memory_celery, fake_slack
) -> None:
    """Proves the check above would see the URL if results were stored."""
    task_ids, writes = run_flows(storing_control_task, fake_slack)

    records = {}
    for key, value in writes:
        records.setdefault(key, []).append(json.loads(value))
    statuses = sorted(r["status"] for rs in records.values() for r in rs)
    assert statuses == ["FAILURE", "FAILURE", "RETRY", "RETRY", "RETRY", "SUCCESS"]

    for name in ("exhausted", "client_error"):
        final = records[f"celery-task-meta-{task_ids[name]}"][-1]
        assert final["status"] == "FAILURE"
        url = f"https://hooks.slack.com/commands/T000/{name}/{TOKEN}"
        assert any(url in message for message in final["result"]["exc_message"])
        assert url in final["traceback"]
        assert TOKEN in json.dumps(final)
