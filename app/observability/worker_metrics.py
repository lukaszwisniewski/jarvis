"""Worker metrics endpoint and the multiprocess directory.

The Celery prefork worker runs tasks in forked child processes, so each
child keeps its own metric values. With PROMETHEUS_MULTIPROC_DIR set,
prometheus_client writes every process's values to files in that directory,
and the endpoint started here (in the worker parent) adds them up.

The directory is cleared only by running this module as a separate command
before Celery starts:

    python -m app.observability.worker_metrics && exec celery ... worker

Nothing inside the worker deletes files, so files the worker parent has
already opened can never be removed. Stale files from an earlier run would
otherwise be added to the new totals.
"""

import glob
import logging
import os
import sys

from prometheus_client import CollectorRegistry, start_http_server
from prometheus_client.multiprocess import MultiProcessCollector

logger = logging.getLogger(__name__)

ENV_VAR = "PROMETHEUS_MULTIPROC_DIR"


def multiproc_dir() -> str | None:
    return os.environ.get(ENV_VAR) or None


def clear_multiproc_dir(path: str) -> int:
    """Delete the metric files (*.db) in `path`. Returns how many were deleted."""
    removed = 0
    for filename in glob.glob(os.path.join(path, "*.db")):
        os.remove(filename)
        removed += 1
    return removed


def start_worker_metrics_server(port: int, start=start_http_server) -> bool:
    """Serve the combined metrics of all worker processes on `port`.

    Does nothing (returns False) when PROMETHEUS_MULTIPROC_DIR is not set,
    so a worker started without it behaves as before. `start` is injectable
    so tests do not open a real port.
    """
    path = multiproc_dir()
    if path is None:
        logger.info("worker metrics endpoint disabled: %s is not set", ENV_VAR)
        return False
    registry = CollectorRegistry()
    MultiProcessCollector(registry, path=path)
    start(port, registry=registry)
    logger.info("worker metrics endpoint listening on port %s", port)
    return True


def main() -> int:
    """Container-start step: create and empty the directory, then exit."""
    path = multiproc_dir()
    if path is None:
        print(f"{ENV_VAR} is not set; nothing to clear")
        return 0
    os.makedirs(path, exist_ok=True)
    removed = clear_multiproc_dir(path)
    print(f"cleared {removed} metric files from {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
