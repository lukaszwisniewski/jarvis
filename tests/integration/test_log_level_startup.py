"""An invalid LOG_LEVEL stops the API and the worker at startup.

Settings() is built when app.config is imported, so the check runs during the
import, before Celery sends any signal. (Celery catches and only logs
exceptions raised in signal receivers, so a check there would not stop the
worker.) Every process runs in a clean environment with all other settings
given explicitly, from a temporary directory so a local .env is never read.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_ERROR = (
    "LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL "
    "(case-insensitive), or empty/unset"
)
TIMEOUT_SECONDS = 30
IN_MEMORY_BROKER = ["-b", "memory://", "--result-backend", "cache+memory://"]


def clean_env(log_level: str) -> dict[str, str]:
    # PROMETHEUS_MULTIPROC_DIR is deliberately absent: the worker then starts
    # no metrics server on port 9100 and writes no metric files.
    return {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_SIGNING_SECRET": "test-signing-secret",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "LLM_STUB_MODE": "true",
        # Nothing listens on port 1, so any broker connection would fail loudly.
        "REDIS_URL": "redis://127.0.0.1:1/0",
        "LOG_LEVEL": log_level,
    }


def run_import(module: str, log_level: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=cwd,
        env=clean_env(log_level),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
    )


@pytest.mark.parametrize("module", ["app.main", "app.workers.celery_app"])
def test_import_succeeds_with_valid_log_level(module: str, tmp_path: Path) -> None:
    result = run_import(module, "INFO", tmp_path)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("module", ["app.main", "app.workers.celery_app"])
def test_import_fails_with_invalid_log_level(module: str, tmp_path: Path) -> None:
    result = run_import(module, "VERBOSE", tmp_path)
    assert result.returncode != 0, result.stderr
    assert "log_level" in result.stderr.lower()
    assert EXPECTED_ERROR in result.stderr, result.stderr


def start_worker(args: list[str], log_level: str, cwd: Path):
    """Start the real Celery CLI in its own process group; stderr goes to a file."""
    stderr_path = cwd / "worker-stderr.log"
    with open(stderr_path, "w") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-m", "celery", "-A", "app.workers.celery_app", *args],
            cwd=cwd,
            env=clean_env(log_level),
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            start_new_session=True,  # the worker and any pool children share a group
        )
    return process, stderr_path


def stop_process_group(process: subprocess.Popen) -> None:
    """Stop the worker and every process it started, even after a failed test.

    Killing only the parent could leave prefork children running.
    """
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # the whole group has already exited
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, 0)  # raises if no process in the group is left
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=10)


def test_worker_starts_with_valid_log_level(tmp_path: Path) -> None:
    # Positive control: with a valid level the worker gets as far as "ready".
    # The in-memory broker makes this independent of Redis.
    process, stderr_path = start_worker(
        [*IN_MEMORY_BROKER, "worker", "--pool", "solo"], "INFO", tmp_path
    )
    try:
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while " ready." not in stderr_path.read_text():
            output = stderr_path.read_text()
            assert process.poll() is None, f"worker exited early:\n{output}"
            assert time.monotonic() < deadline, f"worker not ready:\n{output}"
            time.sleep(0.1)
        assert "Signal handler" not in stderr_path.read_text()
    finally:
        stop_process_group(process)


def test_worker_exits_on_invalid_log_level(tmp_path: Path) -> None:
    # The real command, as in docker-compose.yml, with an unreachable Redis.
    process, stderr_path = start_worker(["worker"], "VERBOSE", tmp_path)
    try:
        try:
            returncode = process.wait(timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            pytest.fail(
                f"worker still running after {TIMEOUT_SECONDS}s; LOG_LEVEL did "
                f"not stop startup:\n{stderr_path.read_text()}"
            )
        output = stderr_path.read_text()
        assert returncode != 0, output
        assert "log_level" in output.lower()
        assert EXPECTED_ERROR in output, output
        # It failed while importing the app, before trying to reach the broker.
        assert "Cannot connect" not in output, output
    finally:
        stop_process_group(process)
