import copy
import json
import os
from pathlib import Path
from typing import Any

import pytest

from scripts.regression_gate import (
    AGENT_POLICY,
    RAG_BASELINE,
    agent_violations,
    rag_violations,
    security_violations,
)

DEV_REPORT = Path("docs/evaluation/evidence/retrieval-v2-dev-final.json")
AGENT_REPORT = Path("docs/evidence/phase2/agent-eval-v1.json")


def load(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text()))


def test_rag_gate_passes_on_the_baseline_report() -> None:
    assert rag_violations(load(DEV_REPORT), load(RAG_BASELINE)) == []


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda r: r["strategies"]["lexical"]["overall"].update({"mrr@5": 0.60}),
            "lexical mrr@5 dropped",
        ),
        (
            lambda r: r["strategies"]["hybrid"]["overall"].update({"recall@5": 0.55}),
            "hybrid recall@5 dropped",
        ),
        (lambda r: r.update({"split": "heldout"}), "dev split"),
        (
            lambda r: r["fingerprint"]["config"].update({"embedding_space": "openai:x:256"}),
            "embedding_space",
        ),
        (lambda r: r["hybrid_diagnostics"].update({"rrf_recomputation_mismatches": 1}), "RRF"),
        (lambda r: r["hybrid_diagnostics"].update({"lexical_branch_empty": 24}), "degenerating"),
        (lambda r: r["strategies"].pop("vector"), "vector: missing"),
    ],
)
def test_rag_gate_detects_material_regressions(mutate: Any, expected: str) -> None:
    report = copy.deepcopy(load(DEV_REPORT))
    mutate(report)
    assert any(expected in violation for violation in rag_violations(report, load(RAG_BASELINE)))


def test_rag_gate_tolerates_less_than_one_query_of_change() -> None:
    report = copy.deepcopy(load(DEV_REPORT))
    baseline = load(RAG_BASELINE)
    lexical = report["strategies"]["lexical"]["overall"]
    lexical["mrr@5"] -= baseline["tolerance"] - 0.001  # within tolerance: reported, not gated
    lexical["ndcg@5"] += 0.2  # improvements never fail
    assert rag_violations(report, baseline) == []


def test_agent_gate_treats_safety_metrics_as_invariants() -> None:
    policy = load(AGENT_POLICY)
    report = load(AGENT_REPORT)
    assert agent_violations(report, policy) == []
    for name in ("unauthorized_action_rate", "approval_bypass_rate"):
        broken = copy.deepcopy(report)
        broken["metrics"][name]["all_cases"]["numerator"] = 1
        assert any(name in v for v in agent_violations(broken, policy))
    duplicate = copy.deepcopy(report)
    duplicate["metrics"]["duplicate_side_effect_rate"]["numerator"] = 1
    assert any("duplicate" in v for v in agent_violations(duplicate, policy))
    weaker = copy.deepcopy(report)
    weaker["metrics"]["task_success_rate"]["rate"] = 15 / 16
    assert any("task_success_rate" in v for v in agent_violations(weaker, policy))
    fewer = copy.deepcopy(report)
    fewer["metrics"]["cases"] = 3
    assert any("cases ran" in v for v in agent_violations(fewer, policy))


def test_security_gate_fails_on_any_failed_or_missing_case() -> None:
    passing: dict[str, Any] = {
        "expected_cases": 2,
        "results": [{"id": "a", "category": "x", "passed": True}] * 2,
    }
    assert security_violations(passing) == []
    failing = copy.deepcopy(passing)
    failing["results"][1] = {"id": "b", "category": "y", "passed": False, "detail": "leak"}
    assert security_violations(failing) == ["b (y): leak"]
    assert security_violations({"expected_cases": 3, "results": passing["results"]})


def test_ci_runs_every_gate() -> None:
    workflow = Path(".github/workflows/ci.yaml").read_text()
    for command in (
        "scripts.regression_gate rag",
        "scripts.regression_gate agent",
        "scripts.regression_gate security",
        "opspilot.benchmark run --split dev",
        "scripts.security_suite",
    ):
        assert command in workflow, command
    assert "--split heldout" not in workflow  # the consumed held-out set is never re-run


@pytest.mark.integration
async def test_security_regression_suite_passes() -> None:
    from scripts.security_suite import run_suite

    report = await run_suite(
        Path("evals/security-v1/cases.json"),
        os.environ["TEST_DATABASE_URL"],
        os.environ["TEST_ADMIN_DATABASE_URL"],
    )
    assert report["expected_cases"] == 10
    assert security_violations(report) == [], report["results"]
