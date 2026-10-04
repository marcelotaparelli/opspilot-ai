"""CI regression gates: RAG quality on the DEV split, agent safety invariants, security suite.

    python -m scripts.regression_gate rag REPORT        # from `opspilot.benchmark run --split dev`
    python -m scripts.regression_gate agent REPORT      # report from `scripts.agent_eval`
    python -m scripts.regression_gate security REPORT   # report from `scripts.security_suite`

Exit status 1 and a JSON list of violations when a gate fails. Thresholds live only in
evals/regression/*.json together with their rationale.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

RAG_BASELINE = Path("evals/regression/rag-dev-baseline.json")
AGENT_POLICY = Path("evals/regression/agent-gate.json")


def rag_violations(report: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    if report.get("split") != "dev":
        violations.append("rag gate only accepts the dev split (held-out is consumed)")
    config = report.get("fingerprint", {}).get("config", {})
    for key in ("k", "embedding_space"):
        if config.get(key) != baseline[key]:
            violations.append(
                f"{key} differs from baseline: {config.get(key)!r} != {baseline[key]!r}"
            )
    tolerance = float(baseline["tolerance"])
    for strategy, metrics in baseline["metrics"].items():
        measured = report.get("strategies", {}).get(strategy, {}).get("overall")
        if measured is None:
            violations.append(f"{strategy}: missing from report")
            continue
        for name, expected in metrics.items():
            drop = float(expected) - float(measured[name])
            if drop > tolerance:
                violations.append(
                    f"{strategy} {name} dropped {drop:.4f} > {tolerance} "
                    f"({expected:.4f} -> {measured[name]:.4f})"
                )
    diagnostics = report.get("hybrid_diagnostics", {})
    invariants = baseline["invariants"]
    if (
        diagnostics.get("rrf_recomputation_mismatches")
        != invariants["rrf_recomputation_mismatches"]
    ):
        violations.append("hybrid output is no longer RRF of both branches")
    if diagnostics.get("lexical_branch_empty", 10**6) > invariants["lexical_branch_empty_max"]:
        violations.append(
            "lexical branch empty on more queries than baseline (hybrid degenerating)"
        )
    return violations


def agent_violations(report: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    metrics = report.get("metrics", {})
    violations: list[str] = []
    if metrics.get("cases", 0) < policy["min_cases"]:
        violations.append(
            f"only {metrics.get('cases', 0)} cases ran; expected >= {policy['min_cases']}"
        )
    for name in policy["invariants_zero"]:
        value = metrics.get(name, {})
        numerator = value.get("all_cases", value).get("numerator")
        if numerator != 0:
            violations.append(f"invariant {name} violated: numerator={numerator}")
    for name, key in (
        ("task_success_rate", "task_success_min"),
        ("terminal_state_correctness", "terminal_state_correctness_min"),
    ):
        rate = metrics.get(name, {}).get("rate")
        if rate is None or rate < policy[key]:
            violations.append(f"{name}={rate} below {policy[key]}")
    return violations


def security_violations(report: dict[str, Any]) -> list[str]:
    results = report.get("results", [])
    violations = [
        f"{row['id']} ({row['category']}): {row.get('detail', 'failed')}"
        for row in results
        if not row["passed"]
    ]
    if len(results) < report.get("expected_cases", 1):
        violations.append("security suite ran fewer cases than declared")
    return violations


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gate", choices=("rag", "agent", "security"))
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    if args.gate == "rag":
        violations = rag_violations(report, json.loads(RAG_BASELINE.read_text()))
    elif args.gate == "agent":
        violations = agent_violations(report, json.loads(AGENT_POLICY.read_text()))
    else:
        violations = security_violations(report)
    print(
        json.dumps(
            {"gate": args.gate, "passed": not violations, "violations": violations}, indent=2
        )
    )
    sys.exit(1 if violations else 0)


if __name__ == "__main__":
    main()
