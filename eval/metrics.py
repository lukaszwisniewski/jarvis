"""Retrieval metrics: recall@k and mean reciprocal rank (MRR).

Document ids look like "github:app/api/orders.py", "jira:ORD-101" or
"confluence:1001"; the part before the colon is the source.
"""

SOURCES = ("github", "jira", "confluence")


def source_of(doc_id: str) -> str:
    return doc_id.split(":", 1)[0]


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    """Share of the relevant documents that appear in the top k results."""
    if not relevant:
        return 0.0
    found = relevant.intersection(ranked[:k])
    return len(found) / len(relevant)


def reciprocal_rank(ranked: list[str], relevant: set[str]) -> float:
    """1 / position of the first relevant result, or 0 if none was retrieved."""
    for position, doc_id in enumerate(ranked, start=1):
        if doc_id in relevant:
            return 1.0 / position
    return 0.0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarise(
    runs: list[tuple[list[str], set[str]]],
    ks: tuple[int, ...] = (5, 10),
) -> dict[str, dict[str, float]]:
    """Average the metrics over all questions, overall and per source.

    Per-source numbers only consider the relevant documents from that source,
    but positions are taken from the final merged ranking. They answer:
    "how well does evidence from this source reach the final context?"
    A question counts towards a source only if it has relevant docs there.
    """
    groups: dict[str, list[tuple[list[str], set[str]]]] = {"overall": runs}
    for source in SOURCES:
        groups[source] = [
            (ranked, {d for d in relevant if source_of(d) == source})
            for ranked, relevant in runs
            if any(source_of(d) == source for d in relevant)
        ]

    report: dict[str, dict[str, float]] = {}
    for name, group in groups.items():
        row: dict[str, float] = {"questions": len(group)}
        for k in ks:
            row[f"recall@{k}"] = _mean([recall_at_k(r, rel, k) for r, rel in group])
        row["mrr"] = _mean([reciprocal_rank(r, rel) for r, rel in group])
        report[name] = row
    return report
