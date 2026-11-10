"""Multiprocess metrics, run in separate Python processes.

prometheus_client chooses multiprocess mode when the first metric is created,
based on PROMETHEUS_MULTIPROC_DIR. The pytest process has already created
metrics without it, so these tests start fresh interpreters with the
variable set. No network is used: the HTTP server start is replaced by a stub.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.observability.worker_metrics import (
    clear_multiproc_dir,
    start_worker_metrics_server,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# Simulates an earlier worker run that left metric files behind.
OLD_RUN = """
from app.observability.metrics import TASK_RETRIES
TASK_RETRIES.labels("slack_timeout").inc(5)
"""

# Simulates a worker: the parent records a value (so it has its own file open),
# runs the worker_init handler, then forks two children that each record one.
WORKER = """
import json, os
from prometheus_client import CollectorRegistry
from prometheus_client.multiprocess import MultiProcessCollector
from app.observability.metrics import TASK_RETRIES
from app.observability.worker_metrics import start_worker_metrics_server

TASK_RETRIES.labels("slack_timeout").inc()
parent_file = f"counter_{os.getpid()}.db"
started = []
start_worker_metrics_server(9100, start=lambda port, registry: started.append(port))
parent_file_after_init = os.path.exists(
    os.path.join(os.environ["PROMETHEUS_MULTIPROC_DIR"], parent_file)
)

children = []
for _ in range(2):
    pid = os.fork()
    if pid == 0:
        TASK_RETRIES.labels("slack_timeout").inc()
        os._exit(0)
    children.append(pid)
for pid in children:
    os.waitpid(pid, 0)

registry = CollectorRegistry()
MultiProcessCollector(registry)
print(json.dumps({
    "total": registry.get_sample_value(
        "jarvis_task_retries_total", {"reason": "slack_timeout"}
    ),
    "started": started,
    "parent_file_after_init": parent_file_after_init,
    "files": sorted(os.listdir(os.environ["PROMETHEUS_MULTIPROC_DIR"])),
}))
"""


def run_python(args: list[str], metrics_dir: Path) -> str:
    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(metrics_dir)}
    result = subprocess.run(
        [sys.executable, *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_wipe_at_start_then_children_are_summed(tmp_path: Path) -> None:
    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir()

    run_python(["-c", OLD_RUN], metrics_dir)
    assert list(metrics_dir.glob("*.db")), "the old run should leave files"

    # Container-start step, as in docker-compose.yml, before the worker runs.
    output = run_python(["-m", "app.observability.worker_metrics"], metrics_dir)
    assert "cleared 1 metric files" in output
    assert not list(metrics_dir.glob("*.db"))

    report = json.loads(
        run_python(["-c", WORKER], metrics_dir).strip().splitlines()[-1]
    )

    # 1 from the parent + 1 from each child; the old run's 5 is gone.
    assert report["total"] == 3.0
    assert report["started"] == [9100]
    # worker_init (start_worker_metrics_server) did not delete the parent's file.
    assert report["parent_file_after_init"] is True
    assert len(report["files"]) == 3  # one counter file per process


def test_clear_only_removes_metric_files(tmp_path: Path) -> None:
    (tmp_path / "counter_1.db").write_bytes(b"x")
    (tmp_path / "histogram_2.db").write_bytes(b"x")
    (tmp_path / "keep.txt").write_text("not a metric file")

    assert clear_multiproc_dir(str(tmp_path)) == 2
    assert [p.name for p in tmp_path.iterdir()] == ["keep.txt"]


def test_endpoint_is_off_without_the_env_var(monkeypatch) -> None:
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    started = []

    assert (
        start_worker_metrics_server(9100, start=lambda *a, **k: started.append(a))
        is False
    )
    assert started == []
