"""OPT-IN live smoke of the approval-gated workflow against a REAL GitLab sandbox project.

Never part of tests or CI. FAILS CLOSED (exit 2, no network) unless ALL are set:
    GITLAB_BASE_URL                         https only, e.g. https://gitlab.com
    GITLAB_TOKEN                            least-privilege project access token (never printed)
    GITLAB_PROJECT_ID                       numeric id of a disposable sandbox project
    OPSPILOT_ALLOW_REAL_GITLAB_SMOKE=true   explicit acknowledgement of a real side effect
    DATABASE_URL                            runtime role of a migrated opspilot database

Flow (in-process app, real PostgreSQL, real GitLab adapter, offline planner/provider):
agent run -> proposal -> approval via the HTTP API -> real REST create -> confirm via GET ->
idempotency re-run (second approval and resume must not create anything) -> close the issue.
The issue carries the marker opspilot-smoke:<uuid>. Evidence records ids, states, HTTP
statuses and request counts only: no token, base URL, web URL or project path.

Token: project access token on a sandbox project, role Reporter (create + close issues),
scope `api` (GitLab has no narrower write scope), shortest practical expiry; revoke after.

    PYTHONPATH=. python -m scripts.live_gitlab_smoke \
        --output docs/evidence/release/live-gitlab-smoke.json
"""

import argparse
import asyncio
import json
import os
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from pydantic import SecretStr

from opspilot.agent.gitlab import GitLabTracker
from opspilot.agent.graph import AgentService
from opspilot.agent.models import idempotency_key, marker_footer
from opspilot.agent.planners import HeuristicPlanner
from opspilot.agent.ports import TrackerUnavailable
from opspilot.agent.store import PostgresAgentStore
from opspilot.api.app import create_app
from opspilot.application import RagService
from opspilot.config import Settings
from opspilot.observability import logs_to_stderr
from opspilot.persistence.postgres import PostgresRepository
from opspilot.providers.fake import FakeProvider

GATE = "OPSPILOT_ALLOW_REAL_GITLAB_SMOKE"
REQUIRED = ("GITLAB_BASE_URL", "GITLAB_TOKEN", "GITLAB_PROJECT_ID", "DATABASE_URL")


def gate_failure(environ: dict[str, str]) -> str | None:
    missing = [name for name in REQUIRED if not environ.get(name)]
    if missing:
        return f"NOT EXECUTED: missing {', '.join(missing)}"
    if environ.get(GATE) != "true":
        return f"NOT EXECUTED: set {GATE}=true to acknowledge a real GitLab side effect"
    if not environ["GITLAB_BASE_URL"].startswith("https://"):
        return "NOT EXECUTED: GITLAB_BASE_URL must use https"
    if not environ["GITLAB_PROJECT_ID"].isdigit():
        return "NOT EXECUTED: GITLAB_PROJECT_ID must be numeric"
    return None


class CountingTransport(httpx.AsyncBaseTransport):
    """Observation only: counts what the adapter sends; never alters or delays it."""

    def __init__(self) -> None:
        self.inner: httpx.AsyncBaseTransport = httpx.AsyncHTTPTransport()
        self.sent: list[tuple[str, int | None]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        index = len(self.sent)
        self.sent.append((request.method, None))  # count attempts even when the response is lost
        response = await self.inner.handle_async_request(request)
        self.sent[index] = (request.method, response.status_code)
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


def owns_issue(body: dict[str, Any], project_id: int, iid: int, description: str | None) -> bool:
    """A search substring alone never authorizes this smoke to close an issue."""
    return (
        description is not None
        and body.get("project_id") == project_id
        and body.get("iid") == iid
        and body.get("description") == description
    )


async def run(
    base_url: str, token: str, project_id: int, database_url: str, allow_http: bool = False
) -> dict[str, Any]:
    tenant, smoke_id = uuid4(), uuid4()
    marker = f"opspilot-smoke:{smoke_id}"
    requester, approver = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    settings = Settings.model_validate(
        {
            "database_url": SecretStr(database_url),
            "tenant_tokens": {
                requester: {
                    "tenant": str(tenant),
                    "subject": "smoke-requester",
                    "roles": ["agent"],
                },
                approver: {
                    "tenant": str(tenant),
                    "subject": "smoke-approver",
                    "roles": ["approver"],
                },
            },
            "agent_policy": {
                "tenants": {
                    str(tenant): {"projects": {"sandbox": {"gitlab_project_id": project_id}}}
                }
            },
            "gitlab_base_url": base_url,
            "gitlab_token": SecretStr(token),
            "gitlab_allow_http": allow_http,
        }
    )
    transport = CountingTransport()
    tracker = GitLabTracker(base_url, token, settings.gitlab_timeout_seconds, transport)
    repository = PostgresRepository(settings)
    fake = FakeProvider()
    rag = RagService(repository, fake, fake)
    agent = AgentService(
        PostgresAgentStore(repository), rag.retriever, HeuristicPlanner(), tracker, settings
    )
    checks: list[dict[str, Any]] = []
    issue: dict[str, Any] = {}
    created_iid: int | None = None
    expected_description: str | None = None
    action_key: str | None = None
    outcome: dict[str, Any] | None = None

    def report(passed: bool) -> dict[str, Any]:
        nonlocal outcome
        outcome = result(checks, issue, transport, marker, project_id, passed)
        return outcome

    gitlab = httpx.AsyncClient(
        base_url=f"{base_url}/api/v4",
        headers={"PRIVATE-TOKEN": token},
        timeout=settings.gitlab_timeout_seconds,
        follow_redirects=False,
    )
    try:
        app = create_app(settings, rag, agent)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://opspilot"
        ) as http:

            def auth(value: str) -> dict[str, str]:
                return {"Authorization": f"Bearer {value}"}

            created = await http.post(
                "/v1/agent/runs",
                headers=auth(requester),
                json={"request": f"Please open an issue: OpsPilot release smoke {marker}"},
            )
            run_view = created.json()
            proposal = run_view.get("proposal") or {}
            checks.append(
                {
                    "step": "proposal",
                    "http": created.status_code,
                    "run_status": run_view.get("status"),
                    "gitlab_requests_before_approval": len(transport.sent),
                }
            )
            if run_view.get("status") != "awaiting_approval" or transport.sent:
                return report(False)
            run_id = run_view["run_id"]
            action_key = idempotency_key(tenant, UUID(run_id), proposal["action_hash"])
            expected_description = proposal["description"] + marker_footer(action_key)
            path = f"/v1/agent/runs/{run_id}"
            approved = await http.post(
                f"{path}/approve",
                headers=auth(approver),
                json={"action_hash": proposal["action_hash"]},
            )
            done = approved.json()
            execution = done.get("execution") or {}
            checks.append(
                {
                    "step": "approve_and_execute",
                    "http": approved.status_code,
                    "run_status": done.get("status"),
                    "gitlab_create_http": next((s for m, s in transport.sent if m == "POST"), None),
                }
            )
            iid = execution.get("issue_iid")
            created_iid = iid if isinstance(iid, int) else None
            if done.get("status") != "succeeded" or not isinstance(iid, int):
                return report(False)

            fetched = await gitlab.get(f"/projects/{project_id}/issues/{iid}")
            body = fetched.json() if fetched.status_code == 200 else {}
            issue = {
                "issue_id": body.get("id"),
                "issue_iid": iid,
                "project_id": body.get("project_id"),
                "state_after_create": body.get("state"),
                "marker_in_description": marker in (body.get("description") or ""),
                "idempotency_marker_in_description": "opspilot-action:"
                in (body.get("description") or ""),
            }
            checks.append({"step": "confirm_via_get", "http": fetched.status_code})
            if fetched.status_code != 200 or not owns_issue(
                body, project_id, iid, expected_description
            ):
                return report(False)

            again = await http.post(
                f"{path}/approve",
                headers=auth(approver),
                json={"action_hash": proposal["action_hash"]},
            )
            resumed = await http.post(f"{path}/resume", headers=auth(requester))
            matches = await tracker.find_by_marker(project_id, marker)
            creates = sum(1 for method, _ in transport.sent if method == "POST")
            checks.append(
                {
                    "step": "idempotency_rerun",
                    "second_approval_http": again.status_code,
                    "resume_http": resumed.status_code,
                    "run_status_after_resume": resumed.json().get("status"),
                    "issues_with_marker": len(matches),
                    "create_requests_sent": creates,
                }
            )

            closed = await gitlab.put(
                f"/projects/{project_id}/issues/{iid}", json={"state_event": "close"}
            )
            final = await gitlab.get(f"/projects/{project_id}/issues/{iid}")
            issue["final_state"] = final.json().get("state") if final.status_code == 200 else None
            created_iid = None if issue["final_state"] == "closed" else created_iid
            checks.append(
                {
                    "step": "close_issue",
                    "http": closed.status_code,
                    "confirm_http": final.status_code,
                }
            )
            passed = (
                created.status_code == 201
                and approved.status_code == 200
                and issue["marker_in_description"]
                and issue["idempotency_marker_in_description"]
                and issue["state_after_create"] == "opened"
                and again.status_code == 409
                and resumed.status_code == 200
                and resumed.json().get("status") == "succeeded"
                and len(matches) == 1
                and creates == 1
                and closed.status_code == 200
                and final.status_code == 200
                and owns_issue(final.json(), project_id, iid, expected_description)
                and issue["final_state"] == "closed"
            )
            return report(passed)
    finally:
        try:
            cleanup_iids = [created_iid] if created_iid is not None else []
            if (
                issue.get("final_state") != "closed"
                and action_key is not None
                and any(m == "POST" for m, _ in transport.sent)
            ):
                # Reconcile the approved action; verify full ownership again before closing.
                try:
                    cleanup_iids.extend(
                        i.iid for i in await tracker.find_by_marker(project_id, action_key)
                    )
                except TrackerUnavailable:
                    checks.append({"step": "cleanup_lookup", "status": "unavailable"})
            for iid_to_close in sorted(set(cleanup_iids)):
                try:
                    candidate = await gitlab.get(f"/projects/{project_id}/issues/{iid_to_close}")
                    if candidate.status_code != 200 or not owns_issue(
                        candidate.json(), project_id, iid_to_close, expected_description
                    ):
                        checks.append(
                            {
                                "step": "cleanup_refused",
                                "issue_iid": iid_to_close,
                                "status": "ownership_not_confirmed",
                            }
                        )
                        continue
                    cleanup = await gitlab.put(
                        f"/projects/{project_id}/issues/{iid_to_close}",
                        json={"state_event": "close"},
                    )
                    checks.append(
                        {
                            "step": "cleanup_close",
                            "issue_iid": iid_to_close,
                            "http": cleanup.status_code,
                        }
                    )
                except httpx.HTTPError:
                    checks.append(
                        {
                            "step": "cleanup_close",
                            "issue_iid": iid_to_close,
                            "status": "unavailable",
                        }
                    )
        finally:
            if outcome is not None:
                # Include reconciliation attempts made after an early return was prepared.
                outcome["adapter_requests"] = [{"method": m, "http": s} for m, s in transport.sent]
            await gitlab.aclose()
            await tracker.close()
            await repository.close()


def result(
    checks: list[dict[str, Any]],
    issue: dict[str, Any],
    transport: CountingTransport,
    marker: str,
    project_id: int,
    passed: bool,
) -> dict[str, Any]:
    return {
        "smoke": "live-gitlab",
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "marker": marker,
        "project_id": project_id,
        "issue": issue,
        "checks": checks,
        "adapter_requests": [{"method": m, "http": s} for m, s in transport.sent],
        "passed": passed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    environ = dict(os.environ)
    refusal = gate_failure(environ)
    if refusal:
        print(refusal)
        sys.exit(2)  # fail closed: nothing external happened
    try:
        with logs_to_stderr():
            outcome = asyncio.run(
                run(
                    environ["GITLAB_BASE_URL"].rstrip("/"),
                    environ["GITLAB_TOKEN"],
                    int(environ["GITLAB_PROJECT_ID"]),
                    environ["DATABASE_URL"],
                )
            )
    except Exception as error:
        # A safe failed report, never a traceback containing secrets or request content.
        outcome = {"smoke": "live-gitlab", "passed": False, "failure_type": type(error).__name__}
    encoded = json.dumps(outcome, indent=2)
    if environ["GITLAB_TOKEN"] in encoded:
        print("REFUSED: evidence would contain the token")
        sys.exit(1)
    if args.output:
        args.output.write_text(encoded + "\n")
    print(encoded)
    sys.exit(0 if outcome["passed"] else 1)


if __name__ == "__main__":
    main()
