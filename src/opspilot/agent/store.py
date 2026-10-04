"""PostgreSQL is the single source of truth for runs, proposals, approvals and executions.

Every statement runs in the existing tenant transaction (transaction-local app.tenant_id +
forced RLS) and also filters tenant_id explicitly. Status transitions are guarded in SQL,
and execution ownership is fenced by an owner token plus lease.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from opspilot.agent.models import (
    AgentState,
    ExecutionStatus,
    IssueAction,
    RunStatus,
    idempotency_key,
)
from opspilot.agent.ports import AgentError, Issue
from opspilot.persistence.postgres import PostgresRepository

Event = tuple[str, str, dict[str, object]]
ClaimMode = Literal["create", "reconcile"]


@dataclass(frozen=True)
class RunRecord:
    id: UUID
    tenant_id: UUID
    requested_by: str
    request: str
    status: RunStatus
    steps: int
    state: AgentState
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ProposalRecord:
    action: IssueAction
    action_hash: str
    created_at: datetime


@dataclass(frozen=True)
class ApprovalRecord:
    decision: Literal["approved", "rejected"]
    action_hash: str
    decided_by: str
    decided_at: datetime


@dataclass(frozen=True)
class ExecutionRecord:
    status: ExecutionStatus
    action_hash: str
    idempotency_key: str
    attempts: int
    owner: UUID | None
    lease_expired: bool
    last_attempt_at: datetime | None
    seconds_since_attempt: float | None
    issue_id: int | None
    issue_iid: int | None
    issue_url: str | None
    last_error: str | None


@dataclass(frozen=True)
class RunBundle:
    run: RunRecord
    proposal: ProposalRecord | None
    approval: ApprovalRecord | None
    execution: ExecutionRecord | None


async def _events(
    connection: AsyncConnection, tenant: UUID, run_id: UUID, events: list[Event]
) -> None:
    for kind, actor, data in events:
        await connection.execute(
            text(
                "INSERT INTO agent_events (run_id, tenant_id, type, actor, data) "
                "VALUES (:run, :tenant, :type, :actor, CAST(:data AS jsonb))"
            ),
            {
                "run": run_id,
                "tenant": tenant,
                "type": kind,
                "actor": actor,
                "data": json.dumps(data),
            },
        )


class PostgresAgentStore:
    def __init__(self, repository: PostgresRepository) -> None:
        self.repository = repository

    async def create_run(self, state: AgentState, events: list[Event]) -> None:
        tenant, run_id = UUID(state["tenant_id"]), UUID(state["run_id"])
        async with self.repository.transaction(tenant) as connection:
            await connection.execute(
                text(
                    "INSERT INTO agent_runs (id, tenant_id, requested_by, request, status, steps, "
                    "state) VALUES (:id, :tenant, :by, :request, :status, :steps, "
                    "CAST(:state AS jsonb))"
                ),
                {
                    "id": run_id,
                    "tenant": tenant,
                    "by": state["subject"],
                    "request": state["request"],
                    "status": state["status"],
                    "steps": state["steps"],
                    "state": json.dumps(state),
                },
            )
            await _events(connection, tenant, run_id, events)

    async def save_planning(self, state: AgentState, events: list[Event]) -> bool:
        """Persist a planning transition; refuses to overwrite a run that left planning."""
        tenant, run_id = UUID(state["tenant_id"]), UUID(state["run_id"])
        async with self.repository.transaction(tenant) as connection:
            result = await connection.execute(
                text(
                    "UPDATE agent_runs SET status=:status, steps=:steps, "
                    "state=CAST(:state AS jsonb), updated_at=now() "
                    "WHERE id=:id AND tenant_id=:tenant AND status='planning'"
                ),
                {
                    "status": state["status"],
                    "steps": state["steps"],
                    "state": json.dumps(state),
                    "id": run_id,
                    "tenant": tenant,
                },
            )
            if result.rowcount != 1:
                return False
            await _events(connection, tenant, run_id, events)
            return True

    async def store_proposal(
        self, state: AgentState, action: IssueAction, events: list[Event]
    ) -> bool:
        tenant, run_id = UUID(state["tenant_id"]), UUID(state["run_id"])
        async with self.repository.transaction(tenant) as connection:
            result = await connection.execute(
                text(
                    "UPDATE agent_runs SET status='awaiting_approval', steps=:steps, "
                    "state=CAST(:state AS jsonb), updated_at=now() "
                    "WHERE id=:id AND tenant_id=:tenant AND status='planning'"
                ),
                {
                    "steps": state["steps"],
                    "state": json.dumps(state),
                    "id": run_id,
                    "tenant": tenant,
                },
            )
            if result.rowcount != 1:
                return False
            await connection.execute(
                text(
                    "INSERT INTO agent_proposals (run_id, tenant_id, action, action_hash) "
                    "VALUES (:run, :tenant, CAST(:action AS jsonb), :hash)"
                ),
                {
                    "run": run_id,
                    "tenant": tenant,
                    "action": action.canonical(),
                    "hash": action.action_hash(),
                },
            )
            await _events(connection, tenant, run_id, events)
            return True

    async def load(self, tenant: UUID, run_id: UUID) -> RunBundle | None:
        async with self.repository.transaction(tenant) as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT r.*, p.action, p.action_hash AS proposal_hash, "
                            "p.created_at AS proposed_at, a.decision, "
                            "a.action_hash AS approved_hash, a.decided_by, a.decided_at, "
                            "e.status AS execution_status, e.action_hash AS execution_hash, "
                            "e.idempotency_key, e.attempts, e.owner, "
                            "e.lease_expires_at < now() AS lease_expired, e.last_attempt_at, "
                            "extract(epoch FROM now() - e.last_attempt_at) AS since_attempt, "
                            "e.external_issue_id, e.external_issue_iid, e.external_url, "
                            "e.last_error FROM agent_runs r "
                            "LEFT JOIN agent_proposals p "
                            "ON p.run_id=r.id AND p.tenant_id=r.tenant_id "
                            "LEFT JOIN agent_approvals a "
                            "ON a.run_id=r.id AND a.tenant_id=r.tenant_id "
                            "LEFT JOIN agent_executions e "
                            "ON e.run_id=r.id AND e.tenant_id=r.tenant_id "
                            "WHERE r.id=:id AND r.tenant_id=:tenant"
                        ),
                        {"id": run_id, "tenant": tenant},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _bundle(dict(row))

    async def events(self, tenant: UUID, run_id: UUID) -> list[dict[str, Any]]:
        async with self.repository.transaction(tenant) as connection:
            rows = await connection.execute(
                text(
                    "SELECT id, at, type, actor, data FROM agent_events "
                    "WHERE run_id=:run AND tenant_id=:tenant ORDER BY id"
                ),
                {"run": run_id, "tenant": tenant},
            )
            return [dict(row) for row in rows.mappings().all()]

    async def decide(
        self,
        tenant: UUID,
        run_id: UUID,
        decided_by: str,
        decision: Literal["approved", "rejected"],
        action_hash: str,
        events: list[Event],
    ) -> None:
        """Record a human decision bound to one exact action hash; serialised per run."""
        async with self.repository.transaction(tenant) as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT r.status, p.action, p.action_hash FROM agent_runs r JOIN "
                            "agent_proposals p ON p.run_id=r.id AND p.tenant_id=r.tenant_id "
                            "WHERE r.id=:id AND r.tenant_id=:tenant FOR UPDATE OF r"
                        ),
                        {"id": run_id, "tenant": tenant},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None or row["status"] != "awaiting_approval":
                raise AgentError("invalid_state")
            stored = IssueAction.model_validate(row["action"])
            if action_hash != row["action_hash"] or stored.action_hash() != row["action_hash"]:
                raise AgentError("action_hash_mismatch")
            try:
                await connection.execute(
                    text(
                        "INSERT INTO agent_approvals (run_id, tenant_id, decision, action_hash, "
                        "decided_by) VALUES (:run, :tenant, :decision, :hash, :by)"
                    ),
                    {
                        "run": run_id,
                        "tenant": tenant,
                        "decision": decision,
                        "hash": action_hash,
                        "by": decided_by,
                    },
                )
            except IntegrityError:
                raise AgentError("already_decided") from None
            if decision == "approved":
                await connection.execute(
                    text(
                        "INSERT INTO agent_executions (run_id, tenant_id, action_hash, "
                        "idempotency_key, status) VALUES (:run, :tenant, :hash, :key, 'pending')"
                    ),
                    {
                        "run": run_id,
                        "tenant": tenant,
                        "hash": action_hash,
                        "key": idempotency_key(tenant, run_id, action_hash),
                    },
                )
            await connection.execute(
                text(
                    "UPDATE agent_runs SET status=:status, updated_at=now() "
                    "WHERE id=:id AND tenant_id=:tenant"
                ),
                {"status": decision, "id": run_id, "tenant": tenant},
            )
            await _events(connection, tenant, run_id, events)

    async def claim(
        self, tenant: UUID, run_id: UUID, owner: UUID, lease_seconds: float
    ) -> ClaimMode | None:
        """Take exclusive ownership of the execution. At most one caller wins a claim."""
        async with self.repository.transaction(tenant) as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT status, lease_expires_at < now() AS expired "
                            "FROM agent_executions "
                            "WHERE run_id=:run AND tenant_id=:tenant FOR UPDATE"
                        ),
                        {"run": run_id, "tenant": tenant},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            mode: ClaimMode
            if row["status"] == "pending":
                mode = "create"
            elif row["status"] == "ambiguous" or (row["status"] == "executing" and row["expired"]):
                # A crashed owner may or may not have sent the request: reconcile first.
                mode = "reconcile"
            else:
                return None
            await connection.execute(
                text(
                    "UPDATE agent_executions SET status='executing', owner=:owner, "
                    "lease_expires_at=now() + make_interval(secs => :lease), updated_at=now() "
                    "WHERE run_id=:run AND tenant_id=:tenant"
                ),
                {"owner": owner, "lease": lease_seconds, "run": run_id, "tenant": tenant},
            )
            await connection.execute(
                text(
                    "UPDATE agent_runs SET status='executing', updated_at=now() "
                    "WHERE id=:id AND tenant_id=:tenant"
                ),
                {"id": run_id, "tenant": tenant},
            )
            await _events(
                connection, tenant, run_id, [("execution.claimed", "system", {"mode": mode})]
            )
            return mode

    async def begin_attempt(self, tenant: UUID, run_id: UUID, owner: UUID) -> bool:
        """Record that a create request is about to be sent (fenced by ownership)."""
        async with self.repository.transaction(tenant) as connection:
            result = await connection.execute(
                text(
                    "UPDATE agent_executions SET attempts=attempts+1, last_attempt_at=now(), "
                    "updated_at=now() WHERE run_id=:run AND tenant_id=:tenant AND owner=:owner "
                    "AND status='executing' AND lease_expires_at > now()"
                ),
                {"run": run_id, "tenant": tenant, "owner": owner},
            )
            if result.rowcount == 1:
                await _events(connection, tenant, run_id, [("execution.attempt", "system", {})])
            return result.rowcount == 1

    async def finish(
        self,
        tenant: UUID,
        run_id: UUID,
        owner: UUID,
        status: ExecutionStatus,
        run_status: RunStatus,
        error: str | None,
        issue: Issue | None,
        events: list[Event],
    ) -> bool:
        """Release ownership with a result. Returns False if ownership was lost."""
        async with self.repository.transaction(tenant) as connection:
            result = await connection.execute(
                text(
                    "UPDATE agent_executions SET status=:status, owner=NULL, "
                    "lease_expires_at=NULL, last_error=:error, "
                    "external_issue_id=:issue_id, external_issue_iid=:iid, "
                    "external_url=:url, updated_at=now() WHERE run_id=:run AND tenant_id=:tenant "
                    "AND owner=:owner AND status='executing'"
                ),
                {
                    "status": status,
                    "error": error,
                    "issue_id": issue.id if issue else None,
                    "iid": issue.iid if issue else None,
                    "url": issue.web_url[:500] if issue else None,
                    "run": run_id,
                    "tenant": tenant,
                    "owner": owner,
                },
            )
            if result.rowcount != 1:
                return False
            await connection.execute(
                text(
                    "UPDATE agent_runs SET status=:status, updated_at=now() "
                    "WHERE id=:id AND tenant_id=:tenant"
                ),
                {"status": run_status, "id": run_id, "tenant": tenant},
            )
            await _events(connection, tenant, run_id, events)
            return True

    async def fail_run(self, tenant: UUID, run_id: UUID, code: str, events: list[Event]) -> None:
        """Terminal failure outside planning (e.g. an approval that no longer matches)."""
        async with self.repository.transaction(tenant) as connection:
            await connection.execute(
                text(
                    "UPDATE agent_runs SET status='failed', state=jsonb_set(state, '{error}', "
                    "to_jsonb(CAST(:code AS text))), updated_at=now() "
                    "WHERE id=:id AND tenant_id=:tenant AND status IN ('approved', 'executing', "
                    "'ambiguous')"
                ),
                {"code": code, "id": run_id, "tenant": tenant},
            )
            await connection.execute(
                text(
                    "UPDATE agent_executions SET status='failed_terminal', last_error=:code, "
                    "owner=NULL, lease_expires_at=NULL, updated_at=now() "
                    "WHERE run_id=:run AND tenant_id=:tenant AND status IN ('pending', 'ambiguous')"
                ),
                {"code": code, "run": run_id, "tenant": tenant},
            )
            await _events(connection, tenant, run_id, events)


def _bundle(row: dict[str, Any]) -> RunBundle:
    run = RunRecord(
        id=row["id"],
        tenant_id=row["tenant_id"],
        requested_by=row["requested_by"],
        request=row["request"],
        status=row["status"],
        steps=row["steps"],
        state=cast(AgentState, row["state"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
    proposal = (
        ProposalRecord(
            IssueAction.model_validate(row["action"]), row["proposal_hash"], row["proposed_at"]
        )
        if row["action"] is not None
        else None
    )
    approval = (
        ApprovalRecord(row["decision"], row["approved_hash"], row["decided_by"], row["decided_at"])
        if row["decision"] is not None
        else None
    )
    execution = (
        ExecutionRecord(
            status=row["execution_status"],
            action_hash=row["execution_hash"],
            idempotency_key=row["idempotency_key"],
            attempts=row["attempts"],
            owner=row["owner"],
            lease_expired=bool(row["lease_expired"]),
            last_attempt_at=row["last_attempt_at"],
            seconds_since_attempt=None
            if row["since_attempt"] is None
            else float(row["since_attempt"]),
            issue_id=row["external_issue_id"],
            issue_iid=row["external_issue_iid"],
            issue_url=row["external_url"],
            last_error=row["last_error"],
        )
        if row["execution_status"] is not None
        else None
    )
    return RunBundle(run, proposal, approval, execution)
