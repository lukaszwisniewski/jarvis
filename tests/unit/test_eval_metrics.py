import pytest

from eval.metrics import recall_at_k, reciprocal_rank, summarise


def test_recall_at_k_counts_relevant_docs_in_top_k() -> None:
    ranked = ["github:a", "jira:X-1", "github:b", "confluence:1"]
    relevant = {"github:b", "confluence:1"}

    assert recall_at_k(ranked, relevant, k=2) == 0.0
    assert recall_at_k(ranked, relevant, k=3) == 0.5
    assert recall_at_k(ranked, relevant, k=10) == 1.0


def test_recall_at_k_with_no_relevant_docs_is_zero() -> None:
    assert recall_at_k(["github:a"], set(), k=5) == 0.0


def test_reciprocal_rank_uses_first_relevant_position() -> None:
    assert reciprocal_rank(["a", "b", "c"], {"c", "b"}) == 0.5
    assert reciprocal_rank(["a", "b"], {"z"}) == 0.0


def test_summarise_overall_and_per_source() -> None:
    runs = [
        # q1: GitHub doc at rank 1, Jira doc missing.
        (["github:a", "github:b"], {"github:a", "jira:X-1"}),
        # q2: only a Jira doc is relevant, found at rank 2.
        (["github:c", "jira:X-2"], {"jira:X-2"}),
    ]

    report = summarise(runs, ks=(1, 10))

    assert report["overall"]["questions"] == 2
    assert report["overall"]["recall@1"] == pytest.approx((0.5 + 0.0) / 2)
    assert report["overall"]["recall@10"] == pytest.approx((0.5 + 1.0) / 2)
    assert report["overall"]["mrr"] == pytest.approx((1.0 + 0.5) / 2)

    assert report["github"]["questions"] == 1
    assert report["github"]["mrr"] == 1.0
    # Jira positions come from the merged ranking: missing in q1, rank 2 in q2.
    assert report["jira"]["questions"] == 2
    assert report["jira"]["mrr"] == pytest.approx((0.0 + 0.5) / 2)
    assert report["confluence"]["questions"] == 0
    assert report["confluence"]["mrr"] == 0.0
