"""Paired comparison of retrieval-v2 strategies from an existing result file.

Reads per-query results only; never runs retrieval, so analysing a held-out report
cannot change it. Bootstrap uses a fixed seed for reproducibility.
"""

import argparse
import json
import random
from pathlib import Path
from typing import Any

PAIRS = (("lexical", "hybrid"), ("lexical", "vector"), ("hybrid", "vector"))
METRICS = ("mrr@5", "recall@5", "ndcg@5")


def paired(rows: list[dict[str, Any]], left: str, right: str, metric: str) -> dict[str, Any]:
    differences = [row["results"][left][metric] - row["results"][right][metric] for row in rows]
    generator = random.Random(0)
    means = sorted(
        sum(generator.choice(differences) for _ in differences) / len(differences)
        for _ in range(10_000)
    )
    return {
        "mean_difference": sum(differences) / len(differences),
        "bootstrap_95ci": [means[249], means[9_749]],
        "wins": sum(1 for value in differences if value > 0),
        "losses": sum(1 for value in differences if value < 0),
        "ties": sum(1 for value in differences if value == 0),
    }


def analyse(report: dict[str, Any]) -> dict[str, Any]:
    rows = report["queries"]
    return {
        "split": report["split"],
        "queries": len(rows),
        "paired_differences": {
            f"{left}-{right}": {metric: paired(rows, left, right, metric) for metric in METRICS}
            for left, right in PAIRS
        },
        "no_relevant_in_top5": {
            mode: [row["id"] for row in rows if row["results"][mode]["recall@5"] == 0]
            for mode in ("lexical", "vector", "hybrid")
        },
        "hybrid_below_lexical_mrr": [
            row["id"]
            for row in rows
            if row["results"]["hybrid"]["mrr@5"] < row["results"]["lexical"]["mrr@5"]
        ],
        "hybrid_above_both_mrr": [
            row["id"]
            for row in rows
            if row["results"]["hybrid"]["mrr@5"]
            > max(row["results"]["lexical"]["mrr@5"], row["results"]["vector"]["mrr@5"])
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    encoded = json.dumps(analyse(json.loads(args.report.read_text())), indent=2)
    if args.output:
        args.output.write_text(encoded + "\n")
    print(encoded)


if __name__ == "__main__":
    main()
