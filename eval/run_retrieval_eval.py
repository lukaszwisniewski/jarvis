"""Offline retrieval evaluation.

Usage:
    poetry run python -m eval.run_retrieval_eval [--output PATH]
        [--min-recall-at-10 FLOAT]

Runs every question in eval/golden.json through the real query generation
(deterministic fallback) and retrieve() against the fake backend, then prints
recall@5, recall@10 and MRR overall and per source.
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from app.retrieval.query_generator import generate_queries
from app.retrieval.retrieval_service import retrieve
from eval.fake_backend import FakeBackend, fake_search_backend
from eval.metrics import summarise

GOLDEN_PATH = Path(__file__).parent / "golden.json"
DEFAULT_OUTPUT = Path(__file__).parent / "results" / "retrieval.json"


async def _run_question(question: dict, backend: FakeBackend) -> dict:
    queries = generate_queries(question["question"])
    results = await retrieve(queries, scope=question.get("scope"))
    ranked = [backend.url_to_id.get(r.url or "", f"unknown:{r.url}") for r in results]
    return {
        "id": question["id"],
        "question": question["question"],
        "queries": queries,
        "ranked": ranked,
        "relevant": question["relevant"],
    }


def run_eval(golden_path: Path = GOLDEN_PATH) -> dict:
    golden = json.loads(golden_path.read_text())
    with fake_search_backend() as backend:
        per_question = [
            asyncio.run(_run_question(q, backend)) for q in golden["questions"]
        ]
    runs = [(q["ranked"], set(q["relevant"])) for q in per_question]
    return {"summary": summarise(runs), "questions": per_question}


def format_table(summary: dict) -> str:
    header = (
        f"{'source':<12}{'questions':>10}{'recall@5':>10}{'recall@10':>11}{'MRR':>8}"
    )
    lines = [header, "-" * len(header)]
    for name, row in summary.items():
        lines.append(
            f"{name:<12}{row['questions']:>10}{row['recall@5']:>10.2f}"
            f"{row['recall@10']:>11.2f}{row['mrr']:>8.2f}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--min-recall-at-10",
        type=float,
        default=None,
        help="Exit with status 1 if overall recall@10 falls below this value.",
    )
    args = parser.parse_args(argv)

    # Retrieval logs at INFO for every query; keep the eval output readable.
    logging.basicConfig(level=logging.ERROR)

    report = run_eval()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")

    print(format_table(report["summary"]))
    print(f"\nPer-question details written to {args.output}")

    overall = report["summary"]["overall"]["recall@10"]
    if args.min_recall_at_10 is not None and overall < args.min_recall_at_10:
        print(
            f"FAIL: overall recall@10 {overall:.2f} < {args.min_recall_at_10:.2f}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
