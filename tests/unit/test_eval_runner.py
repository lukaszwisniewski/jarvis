import json

import httpx
import pytest

from app.config import settings
from eval.fake_backend import FakeBackend, fake_search_backend
from eval.run_retrieval_eval import main, run_eval


def test_offline_eval_runs_and_metrics_are_in_range() -> None:
    report = run_eval()

    assert len(report["questions"]) == 20
    for row in report["summary"].values():
        for name in ("recall@5", "recall@10", "mrr"):
            assert 0.0 <= row[name] <= 1.0
    # Every retrieved result maps back to a corpus document.
    for q in report["questions"]:
        assert not any(doc_id.startswith("unknown:") for doc_id in q["ranked"])


def test_fake_backend_rejects_unknown_urls() -> None:
    # Guards the "no network in the eval" promise: anything the fake backend
    # does not recognise fails instead of being sent anywhere.
    backend = FakeBackend()
    with pytest.raises(AssertionError, match="unexpected URL"):
        backend.handle(httpx.Request("GET", "https://example.com/anything"))


def test_fake_backend_restores_settings() -> None:
    before = (settings.llm_stub_mode, list(settings.github_repos))
    with fake_search_backend():
        assert settings.llm_stub_mode is True
    assert (settings.llm_stub_mode, list(settings.github_repos)) == before


def test_min_recall_gate_fails_below_threshold(tmp_path) -> None:
    output = tmp_path / "retrieval.json"

    assert main(["--output", str(output), "--min-recall-at-10", "0.0"]) == 0
    assert main(["--output", str(output), "--min-recall-at-10", "1.01"]) == 1
    assert "summary" in json.loads(output.read_text())
