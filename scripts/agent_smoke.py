"""HTTP smoke of the agent workflow against a running stack (compose.smoke.yaml).

Checks the approval gate with real sockets: no issue before approval, wrong hash and
self-approval refused, exactly one issue after approval. Output excludes credentials and
request/document text.
"""

import argparse
import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def call(
    base: str, method: str, path: str, token: str = "", body: object = None
) -> tuple[int, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = Request(base + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"null")
    except HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"agent smoke failed: {message}")


def smoke(base: str, gitlab: str, requester: str, approver: str) -> dict[str, object]:
    checks: list[dict[str, object]] = []
    status, _ = call(
        base,
        "POST",
        "/v1/documents",
        requester,
        {
            "content": "Payments restart runbook: drain traffic, restart payments-api.",
            "metadata": {"title": "Payments restart"},
        },
    )
    expect(status == 201, "ingest")
    status, _ = call(gitlab, "POST", "/_control/reset", body={})
    expect(status == 200, "fake GitLab reset")

    status, run = call(
        base,
        "POST",
        "/v1/agent/runs",
        requester,
        {"request": "Please open an issue to restart payments-api"},
    )
    expect(status == 201 and run["status"] == "awaiting_approval" and run["proposal"], "proposal")
    run_id, action_hash = run["run_id"], run["proposal"]["action_hash"]
    checks.append(
        {
            "step": "create_run",
            "status": status,
            "run_status": run["status"],
            "request_id": run["request_id"],
        }
    )
    _, issues = call(gitlab, "GET", "/_control/issues")
    expect(issues["creates_received"] == 0, "no side effect before approval")

    for name, token, body, code in (
        ("self_approval", requester, {"action_hash": action_hash}, 403),
        ("wrong_hash", approver, {"action_hash": "0" * 64}, 409),
    ):
        status, error = call(base, "POST", f"/v1/agent/runs/{run_id}/approve", token, body)
        expect(status == code, name)
        checks.append({"step": name, "status": status, "error": error["error"]})
    _, issues = call(gitlab, "GET", "/_control/issues")
    expect(issues["creates_received"] == 0, "refused approvals executed nothing")

    status, done = call(
        base, "POST", f"/v1/agent/runs/{run_id}/approve", approver, {"action_hash": action_hash}
    )
    expect(
        status == 200 and done["status"] == "succeeded" and done["execution"]["issue_url"],
        "execution",
    )
    status, again = call(
        base, "POST", f"/v1/agent/runs/{run_id}/approve", approver, {"action_hash": action_hash}
    )
    expect(status == 409, "second approval refused")
    status, fetched = call(base, "GET", f"/v1/agent/runs/{run_id}", requester)
    expect(status == 200 and fetched["status"] == "succeeded", "get run")
    _, issues = call(gitlab, "GET", "/_control/issues")
    expect(issues["creates_received"] == 1 and len(issues["issues"]) == 1, "exactly one issue")
    expect("opspilot-action:" in issues["issues"][0]["description"], "idempotency marker")
    checks.append(
        {
            "step": "approve",
            "status": 200,
            "run_status": done["status"],
            "approved_hash_matches": done["approval"]["action_hash"] == action_hash,
            "issue_iid": done["execution"]["issue_iid"],
            "gitlab_creates_received": issues["creates_received"],
        }
    )
    status, answer = call(
        base, "POST", "/v1/agent/runs", requester, {"request": "How do I restart payments-api?"}
    )
    expect(
        status == 201 and answer["status"] == "answered" and answer["cited_chunk_ids"],
        "rag-only run",
    )
    checks.append({"step": "question_run", "status": status, "run_status": answer["status"]})
    return {
        "transport": "HTTP socket",
        "provider": "fake",
        "gitlab": "fake server via real adapter",
        "checks": checks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--gitlab-url", default="http://127.0.0.1:8081")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = smoke(
        args.base_url,
        args.gitlab_url,
        os.environ["OPSPILOT_TOKEN"],
        os.environ["OPSPILOT_APPROVER_TOKEN"],
    )
    encoded = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n")
    print(encoded)


if __name__ == "__main__":
    main()
