"""Agent workflow against real PostgreSQL (RLS) and the real GitLab adapter over HTTP."""

import asyncio
import os
from typing import Any, TypeVar
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from opspilot.agent.graph import AgentService
from opspilot.agent.planners import ScriptedPlanner
from opspilot.agent.policy import Denied, Principal
from opspilot.agent.ports import AgentError
from opspilot.agent.store import PostgresAgentStore, RunBundle
from opspilot.domain import DependencyError
from opspilot.persistence.postgres import PostgresRepository
from tests.agent_support import (
    INJECTION_APPROVAL,
    INJECTION_PROJECT,
    SEARCH,
    Env,
    prepare,
    service,
)

pytestmark = pytest.mark.integration
ISSUE_REQUEST = "Please open an issue to restart payments-api"
T = TypeVar("T")


def must(value: T | None) -> T:
    assert value is not None
    return value


async def event_types(svc: Any, env: Env, run_id: UUID) -> list[str]:
    return [event["type"] for event in await svc.store.events(env.a, run_id)]


async def admin(sql: str, params: dict[str, object]) -> None:
    engine = create_async_engine(os.environ["TEST_ADMIN_DATABASE_URL"])
    try:
        async with engine.begin() as connection:
            await connection.execute(text(sql), params)
    finally:
        await engine.dispose()


# ------------------------------------------------------------------ happy paths (A, B, C)


async def test_rag_only_question_answers_without_tools_or_proposal(agent_env: Env) -> None:
    async with service(agent_env) as svc:
        bundle = await svc.start(agent_env.alice, "How do I restart payments-api?")
    assert bundle.run.status == "answered" and bundle.proposal is None
    cited = bundle.run.state["cited_chunk_ids"]
    assert cited and set(cited) <= {ref["chunk_id"] for ref in bundle.run.state["context"]}
    assert agent_env.server.state.creates_received == 0


async def test_issue_is_proposed_approved_executed_once_with_full_audit(agent_env: Env) -> None:
    async with service(agent_env) as svc:
        proposed = await svc.start(agent_env.alice, ISSUE_REQUEST)
        assert proposed.run.status == "awaiting_approval" and proposed.proposal is not None
        assert agent_env.server.state.creates_received == 0  # prepare is side-effect free
        action = must(proposed.proposal).action
        assert (action.project, action.project_id, action.requested_by) == (
            "payments-ops",
            101,
            "alice",
        )
        assert must(proposed.proposal).action_hash == action.action_hash()
        done = await svc.decide(
            agent_env.bob, proposed.run.id, must(proposed.proposal).action_hash, True
        )
        events = await svc.store.events(agent_env.a, done.run.id)
    assert done.run.status == "succeeded" and done.execution is not None
    assert must(done.execution).status == "succeeded" and must(done.execution).issue_iid == 1
    (issue,) = agent_env.issues()
    assert issue["title"] == action.title
    assert issue["description"].startswith(action.description)
    assert must(done.execution).idempotency_key in issue["description"]
    by_type = {event["type"]: event for event in events}
    # Audit: who started, what was proposed, who approved which hash, what happened.
    assert by_type["run.created"]["actor"] == "alice"
    assert by_type["llm.decision"]["actor"] == "model"
    assert by_type["proposal.stored"]["data"]["action_hash"] == action.action_hash()
    assert by_type["approval.approved"]["actor"] == "bob"
    assert by_type["approval.approved"]["data"]["action_hash"] == action.action_hash()
    assert by_type["execution.succeeded"]["data"]["issue_url"] == issue["web_url"]
    serialised = str(events)
    assert "fake-gitlab-token-value" not in serialised and "Authorization" not in serialised


async def test_rejection_is_terminal_and_executes_nothing(agent_env: Env) -> None:
    async with service(agent_env) as svc:
        proposed = await svc.start(agent_env.alice, ISSUE_REQUEST)
        assert proposed.proposal is not None
        rejected = await svc.decide(
            agent_env.bob, proposed.run.id, must(proposed.proposal).action_hash, False
        )
        resumed = await svc.resume(agent_env.alice, proposed.run.id)
    assert rejected.run.status == resumed.run.status == "rejected"
    assert agent_env.server.state.creates_received == 0


# ------------------------------------------------------------------ approval bypass (D)


async def test_no_side_effect_without_a_valid_approval(agent_env: Env) -> None:
    async with service(agent_env) as svc:
        proposed = await svc.start(agent_env.alice, ISSUE_REQUEST)
        assert proposed.proposal is not None
        run, good = proposed.run.id, must(proposed.proposal).action_hash
        # Resume before approval does nothing.
        assert (await svc.resume(agent_env.alice, run)).run.status == "awaiting_approval"
        with pytest.raises(AgentError, match="action_hash_mismatch"):
            await svc.decide(agent_env.bob, run, "0" * 64, True)
        with pytest.raises(Denied, match="approver_role_required"):
            await svc.decide(agent_env.alice, run, good, True)
        self_approver = Principal(
            tenant=agent_env.a, subject="alice", roles=frozenset({"agent", "approver"})
        )
        with pytest.raises(Denied, match="self_approval_not_allowed"):
            await svc.decide(self_approver, run, good, True)
        with pytest.raises(AgentError, match="not_found"):
            await svc.decide(agent_env.mallory, run, good, True)  # other tenant: invisible
        assert (await svc.get(agent_env.alice, run)).approval is None
    assert agent_env.server.state.creates_received == 0


async def test_changed_action_invalidates_approval_before_execution(agent_env: Env) -> None:
    async with service(agent_env) as svc:
        proposed = await svc.start(agent_env.alice, ISSUE_REQUEST)
        assert proposed.proposal is not None
        run, good = proposed.run.id, must(proposed.proposal).action_hash
        # Process records the approval, then (before executing) the stored action changes.
        await svc.store.decide(agent_env.a, run, "bob", "approved", good, [])
        await admin(
            "UPDATE agent_proposals SET action = jsonb_set(action, '{title}', '\"Delete prod\"') "
            "WHERE run_id = :run",
            {"run": run},
        )
        after = await svc.resume(agent_env.bob, run)
        events = await event_types(svc, agent_env, run)
    assert after.run.status == "failed" and after.run.state["error"] == "approval_invalid"
    assert "approval.invalid" in events
    assert agent_env.server.state.creates_received == 0


async def test_policy_is_rechecked_at_execution_time(agent_env: Env) -> None:
    async with service(agent_env) as svc:
        proposed = await svc.start(agent_env.alice, ISSUE_REQUEST)
        assert proposed.proposal is not None
        await svc.store.decide(
            agent_env.a, proposed.run.id, "bob", "approved", must(proposed.proposal).action_hash, []
        )
    narrowed: dict[str, object] = {"tenants": {str(agent_env.a): {"projects": {}}}}
    async with service(agent_env, agent_policy=narrowed) as svc:
        after = await svc.resume(agent_env.bob, proposed.run.id)
    assert after.run.status == "failed" and after.run.state["error"] == "project_not_allowed"
    assert agent_env.server.state.creates_received == 0


# ------------------------------------------------------------- authorization / injection (E, F)


async def test_unauthorized_project_is_never_proposed(agent_env: Env) -> None:
    script = [
        SEARCH,
        prepare("secret-admin"),
        prepare("b-ops"),
        {"tool": "final_answer", "answer": "Blocked.", "cited_chunk_ids": []},
    ]
    async with service(agent_env, script) as svc:
        bundle = await svc.start(agent_env.alice, ISSUE_REQUEST)
        events = await svc.store.events(agent_env.a, bundle.run.id)
    assert bundle.run.status == "answered" and bundle.proposal is None
    denials = [
        {key: value for key, value in event["data"].items() if key != "trace_id"}
        for event in events
        if event["type"] == "policy.denied"
    ]
    assert denials == [
        {"code": "project_not_allowed", "project": "secret-admin"},
        {"code": "project_not_allowed", "project": "b-ops"},
    ]
    assert agent_env.server.state.creates_received == 0


async def test_prompt_injection_cannot_enable_tools_projects_or_skip_approval(
    agent_env: Env,
) -> None:
    # A fully compromised model obeys the injected documents.
    script = [
        SEARCH,
        {
            "tool": "create_gitlab_issue",
            "project": "payments-ops",
            "title": "x",
            "description": "y",
        },
        prepare("secret-admin"),
        {"tool": "approve", "action_hash": "f" * 64},
        prepare(),
    ]
    planner = ScriptedPlanner(script)
    async with service(agent_env, planner) as svc:
        bundle = await svc.start(agent_env.alice, ISSUE_REQUEST)
        events = await event_types(svc, agent_env, bundle.run.id)
    observations = str(planner.views[1].observations)
    assert INJECTION_APPROVAL in observations or INJECTION_PROJECT in observations  # read as data
    assert events.count("tool.denied") == 2  # create_gitlab_issue and "approve" refused
    assert "policy.denied" in events
    assert bundle.run.status == "awaiting_approval" and bundle.proposal is not None
    assert must(bundle.proposal).action.project == "payments-ops" and bundle.approval is None
    assert agent_env.server.state.creates_received == 0
    # The offline heuristic planner is not steered by the injected text either.
    async with service(agent_env) as svc:
        heuristic = await svc.start(agent_env.alice, ISSUE_REQUEST)
    assert heuristic.run.status == "awaiting_approval" and heuristic.proposal is not None
    assert must(heuristic.proposal).action.project == "payments-ops"
    assert agent_env.server.state.creates_received == 0


async def test_retrieval_inside_the_agent_stays_tenant_scoped(agent_env: Env) -> None:
    async with service(agent_env) as svc:
        bundle = await svc.start(agent_env.alice, "PRIVATE_B_RUNBOOK payments restart")
        other = await svc.start(agent_env.mallory, "PRIVATE_B_RUNBOOK payments restart")
        with pytest.raises(AgentError, match="not_found"):
            await svc.get(agent_env.mallory, bundle.run.id)
    titles = {ref["title"] for ref in bundle.run.state["context"]}
    assert titles and "B" not in titles
    assert {ref["title"] for ref in other.run.state["context"]} == {"B"}


async def test_explicit_tenant_filters_hold_even_when_rls_is_bypassed(agent_env: Env) -> None:
    async with service(agent_env) as svc:
        bundle = await svc.start(agent_env.alice, ISSUE_REQUEST)
    admin_url = SecretStr(os.environ["TEST_ADMIN_DATABASE_URL"])
    admin_store = PostgresAgentStore(
        PostgresRepository(agent_env.settings().model_copy(update={"database_url": admin_url}))
    )
    try:
        assert await admin_store.load(agent_env.b, bundle.run.id) is None
        assert await admin_store.events(agent_env.b, bundle.run.id) == []
        assert await admin_store.load(agent_env.a, bundle.run.id) is not None
    finally:
        await admin_store.repository.close()


# ------------------------------------------------------------------ model failures (G, H)


async def test_malformed_arguments_are_bounded_and_never_executed(agent_env: Env) -> None:
    script = [
        {"tool": "prepare_gitlab_issue", "project": "payments-ops"},
        {"tool": "search_knowledge", "query": ""},
        "free text instead of JSON",
    ]
    async with service(agent_env, script) as svc:
        bundle = await svc.start(agent_env.alice, ISSUE_REQUEST)
        events = await event_types(svc, agent_env, bundle.run.id)
    assert bundle.run.status == "failed" and bundle.run.state["error"] == "invalid_model_output"
    assert events.count("decision.invalid") == 3 and bundle.proposal is None


async def test_repeated_tool_loop_stops_exactly_at_max_steps(agent_env: Env) -> None:
    planner = ScriptedPlanner([SEARCH], repeat_last=True)
    async with service(agent_env, planner, agent_max_steps=4) as svc:
        bundle = await svc.start(agent_env.alice, "loop forever")
    assert bundle.run.status == "failed" and bundle.run.state["error"] == "step_limit"
    assert bundle.run.steps == 4 and planner.calls == 4


async def test_llm_timeout_and_workflow_deadline_are_controlled(agent_env: Env) -> None:
    async with service(
        agent_env, ScriptedPlanner([SEARCH], delay=3), agent_llm_timeout_seconds=0.2
    ) as svc:
        slow = await svc.start(agent_env.alice, "anything")
    assert slow.run.status == "failed" and slow.run.state["error"] == "llm_unavailable"
    async with service(
        agent_env,
        ScriptedPlanner([SEARCH], repeat_last=True, delay=0.4),
        agent_deadline_seconds=1,
        agent_max_steps=20,
    ) as svc:
        late = await svc.start(agent_env.alice, "anything")
    assert late.run.status == "failed" and late.run.state["error"] == "deadline_exceeded"


# ------------------------------------------------------------------ GitLab outcomes (I, J)


async def approved(svc: AgentService, env: Env) -> RunBundle:
    proposed = await svc.start(env.alice, ISSUE_REQUEST)
    return await svc.decide(env.bob, proposed.run.id, must(proposed.proposal).action_hash, True)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409])
async def test_terminal_gitlab_errors_do_not_retry(agent_env: Env, status: int) -> None:
    agent_env.fault(f"status:{status}")
    async with service(agent_env) as svc:
        done = await approved(svc, agent_env)
        again = await svc.resume(agent_env.alice, done.run.id)
    assert done.run.status == again.run.status == "failed"
    execution = must(done.execution)
    assert execution.status == "failed_terminal"
    assert must(execution.last_error).startswith("gitlab_")
    assert agent_env.server.state.creates_received == 1 and agent_env.issues() == []


async def test_rate_limit_is_retryable_and_creates_once(agent_env: Env) -> None:
    agent_env.fault("status:429")
    async with service(agent_env) as svc:
        first = await approved(svc, agent_env)
        assert first.run.status == "approved" and must(first.execution).status == "pending"
        second = await svc.resume(agent_env.alice, first.run.id)
    assert second.run.status == "succeeded" and len(agent_env.issues()) == 1


async def test_connection_failure_before_send_is_safe_to_retry(agent_env: Env) -> None:
    async with service(agent_env, tracker_url="http://127.0.0.1:9") as svc:
        first = await approved(svc, agent_env)
    assert (
        must(first.execution).status == "pending"
        and must(first.execution).last_error == "gitlab_connect_failed"
    )
    async with service(agent_env) as svc:
        second = await svc.resume(agent_env.bob, first.run.id)
    assert second.run.status == "succeeded" and len(agent_env.issues()) == 1


@pytest.mark.parametrize(
    "fault", ["drop_after_create", "malformed_after_create", "hold_after_create:3"]
)
async def test_ambiguous_result_after_side_effect_reconciles_without_duplicate(
    agent_env: Env, fault: str
) -> None:
    agent_env.fault(fault)
    async with service(agent_env) as svc:
        first = await approved(svc, agent_env)
        assert first.run.status == "ambiguous" and must(first.execution).status == "ambiguous"
        assert len(agent_env.issues()) == 1  # GitLab did create it
        second = await svc.resume(agent_env.alice, first.run.id)
        events = await event_types(svc, agent_env, first.run.id)
    assert second.run.status == "succeeded" and must(second.execution).issue_iid == 1
    assert len(agent_env.issues()) == 1 and agent_env.server.state.creates_received == 1
    assert "execution.reconciled" in events


async def test_ambiguous_without_side_effect_resends_once_after_grace(agent_env: Env) -> None:
    agent_env.fault("status:503")  # answered 5xx without creating: unknown to the client
    async with service(agent_env, reconcile_grace_seconds=3600) as svc:
        first = await approved(svc, agent_env)
        early = await svc.resume(agent_env.alice, first.run.id)
    assert first.run.status == early.run.status == "ambiguous"
    assert agent_env.server.state.creates_received == 1  # grace not elapsed: no resend
    async with service(agent_env) as svc:
        late = await svc.resume(agent_env.alice, first.run.id)
    assert late.run.status == "succeeded" and must(late.execution).attempts == 2
    assert len(agent_env.issues()) == 1


async def test_attempt_budget_ends_in_terminal_failure(agent_env: Env) -> None:
    agent_env.fault("status:503", times=10)
    async with service(agent_env, max_execution_attempts=2) as svc:
        first = await approved(svc, agent_env)
        await svc.resume(agent_env.alice, first.run.id)
        last = await svc.resume(agent_env.alice, first.run.id)
    assert last.run.status == "failed" and must(last.execution).status == "failed_terminal"
    assert must(last.execution).last_error == "attempts_exhausted"
    assert agent_env.server.state.creates_received == 2


async def test_database_failure_after_side_effect_recovers_without_duplicate(
    agent_env: Env,
) -> None:
    async with service(agent_env, execution_lease_seconds=1.5, gitlab_timeout_seconds=0.5) as svc:
        proposed = await svc.start(agent_env.alice, ISSUE_REQUEST)
        original = svc.store.finish

        async def broken_finish(*args: Any, **kwargs: Any) -> bool:
            raise DependencyError

        svc.store.finish = broken_finish  # type: ignore[method-assign]
        with pytest.raises(DependencyError):
            await svc.decide(
                agent_env.bob, proposed.run.id, must(proposed.proposal).action_hash, True
            )
        svc.store.finish = original  # type: ignore[method-assign]
        stuck = await svc.get(agent_env.alice, proposed.run.id)
        assert must(stuck.execution).status == "executing" and len(agent_env.issues()) == 1
        still = await svc.resume(agent_env.alice, proposed.run.id)
        assert must(still.execution).status == "executing"  # live lease: nobody else may act
        await asyncio.sleep(1.7)  # lease expiry: the owner is presumed dead
        recovered = await svc.resume(agent_env.alice, proposed.run.id)
    assert recovered.run.status == "succeeded" and len(agent_env.issues()) == 1


async def test_checkpoint_store_unavailable_is_a_controlled_error(agent_env: Env) -> None:
    async with service(
        agent_env, repository_url="postgresql+asyncpg://opspilot_app:x@127.0.0.1:9/none"
    ) as svc:
        with pytest.raises(DependencyError):
            await svc.start(agent_env.alice, ISSUE_REQUEST)
    assert agent_env.server.state.creates_received == 0


# ------------------------------------------------------------------ restart (K, L) and concurrency


async def test_restart_between_proposal_and_approval(agent_env: Env) -> None:
    async with service(agent_env) as process_a:
        proposed = await process_a.start(agent_env.alice, ISSUE_REQUEST)
    # Process A is gone: new engine, new graph, nothing shared in memory.
    async with service(agent_env) as process_b:
        reopened = await process_b.get(agent_env.bob, proposed.run.id)
        assert reopened.proposal is not None and reopened.run.status == "awaiting_approval"
        done = await process_b.decide(
            agent_env.bob, proposed.run.id, must(reopened.proposal).action_hash, True
        )
    assert done.run.status == "succeeded" and len(agent_env.issues()) == 1


async def test_restart_between_approval_and_execution(agent_env: Env) -> None:
    async with service(agent_env) as process_a:
        proposed = await process_a.start(agent_env.alice, ISSUE_REQUEST)
        # The approval transaction commits; the process dies before executing.
        await process_a.store.decide(
            agent_env.a, proposed.run.id, "bob", "approved", must(proposed.proposal).action_hash, []
        )
    assert agent_env.server.state.creates_received == 0
    async with service(agent_env) as process_b:
        done = await process_b.resume(agent_env.alice, proposed.run.id)
    assert done.run.status == "succeeded" and len(agent_env.issues()) == 1


async def test_concurrent_approvals_produce_one_owner_and_one_issue(agent_env: Env) -> None:
    agent_env.fault("hold_after_create:0.5")
    async with service(agent_env) as svc:
        proposed = await svc.start(agent_env.alice, ISSUE_REQUEST)
        carol = Principal(tenant=agent_env.a, subject="carol", roles=frozenset({"approver"}))
        results = await asyncio.gather(
            svc.decide(agent_env.bob, proposed.run.id, must(proposed.proposal).action_hash, True),
            svc.decide(carol, proposed.run.id, must(proposed.proposal).action_hash, True),
            return_exceptions=True,
        )
        final = await svc.get(agent_env.alice, proposed.run.id)
    errors = [result for result in results if isinstance(result, Exception)]
    assert len(errors) == 1 and isinstance(errors[0], AgentError)
    assert final.run.status == "succeeded" and agent_env.server.state.creates_received == 1


async def test_concurrent_resumes_claim_execution_once(agent_env: Env) -> None:
    agent_env.fault("status:429")
    async with service(agent_env) as svc:
        first = await approved(svc, agent_env)
        assert must(first.execution).status == "pending"
        agent_env.fault("hold_after_create:0.5")
        await asyncio.gather(*(svc.resume(agent_env.alice, first.run.id) for _ in range(5)))
        final = await svc.get(agent_env.alice, first.run.id)
    assert final.run.status == "succeeded"
    assert agent_env.server.state.creates_received == 2 and len(agent_env.issues()) == 1


async def test_runtime_role_cannot_rewrite_approvals_or_audit(agent_env: Env) -> None:
    async with service(agent_env) as svc:
        done = await approved(svc, agent_env)
        for sql in (
            "UPDATE agent_approvals SET action_hash = repeat('0', 64)",
            "UPDATE agent_proposals SET action_hash = repeat('0', 64)",
            "DELETE FROM agent_events",
        ):
            with pytest.raises(DependencyError):
                async with svc.store.repository.transaction(agent_env.a) as connection:
                    await connection.execute(text(sql))
        after = await svc.get(agent_env.alice, done.run.id)
    assert (
        after.approval is not None
        and must(after.approval).action_hash == must(done.approval).action_hash
    )


async def test_agent_tables_are_isolated_by_rls_without_predicates(agent_env: Env) -> None:
    async with service(agent_env) as svc:
        done = await approved(svc, agent_env)
        run = done.run.id
        # Tenant B context, deliberately no tenant predicate: only RLS stands in the way.
        async with svc.store.repository.transaction(agent_env.b) as connection:
            for table, key in (
                ("agent_runs", "id"),
                ("agent_proposals", "run_id"),
                ("agent_approvals", "run_id"),
                ("agent_executions", "run_id"),
                ("agent_events", "run_id"),
            ):
                count = await connection.execute(
                    text(f"SELECT count(*) FROM {table} WHERE {key} = :run"), {"run": run}
                )
                assert count.scalar_one() == 0, table
        # Tenant B cannot write into tenant A's run either (WITH CHECK).
        with pytest.raises(DependencyError):
            async with svc.store.repository.transaction(agent_env.b) as connection:
                await connection.execute(
                    text(
                        "INSERT INTO agent_events (run_id, tenant_id, type, actor) "
                        "VALUES (:run, :tenant, 'forged', 'mallory')"
                    ),
                    {"run": run, "tenant": agent_env.a},
                )


async def test_unconfigured_gitlab_fails_closed_after_approval(agent_env: Env) -> None:
    async with service(agent_env, with_tracker=False) as svc:
        done = await approved(svc, agent_env)
    assert done.run.status == "failed" and done.run.state["error"] == "gitlab_unconfigured"
    assert must(done.execution).status == "failed_terminal"
    assert agent_env.server.state.creates_received == 0


async def test_restart_during_planning_resumes_from_persisted_state(agent_env: Env) -> None:
    from uuid import uuid4

    from opspilot.agent.models import AgentState

    state = AgentState(
        run_id=str(uuid4()),
        tenant_id=str(agent_env.a),
        subject="alice",
        request=ISSUE_REQUEST,
        status="planning",
        steps=1,
        invalid_outputs=0,
        observations=[],
        context=[],
        decision=None,
        answer=None,
        cited_chunk_ids=[],
        error=None,
    )
    async with service(agent_env) as process_a:
        # Process A persisted the run (one step done) and died mid-planning.
        await process_a.store.create_run(state, [])
    async with service(agent_env) as process_b:
        resumed = await process_b.resume(agent_env.alice, UUID(state["run_id"]))
    assert resumed.run.status == "awaiting_approval" and resumed.proposal is not None
    assert resumed.run.steps == 3  # continued from the persisted step count
    assert agent_env.server.state.creates_received == 0
