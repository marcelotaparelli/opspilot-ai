"""Bounded LangGraph workflow with durable, approval-gated execution.

Topology (see docs/architecture/agent-workflow.md):

    START -> route -> plan <-> {search_knowledge, prepare_gitlab_issue, final_answer}
    START -> route -> execute          (only for persisted status approved/executing/ambiguous)

There is no edge from planning to execution: the only way to reach `execute` is a new
invocation whose persisted status was set by a human approval transaction. Every node
persists its transition before returning, so PostgreSQL always holds the latest state.
"""

import asyncio
from typing import Literal, cast
from uuid import UUID, uuid4

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import ValidationError

from opspilot.agent.models import (
    DECISION,
    AgentState,
    FinalAnswer,
    PrepareGitLabIssue,
    SearchKnowledge,
    marker_footer,
)
from opspilot.agent.policy import (
    Denied,
    Principal,
    authorize_decision,
    authorize_execution,
    authorize_issue,
    authorize_start,
    authorize_tool,
)
from opspilot.agent.ports import (
    AgentError,
    CreateOutcome,
    IssueTracker,
    Planner,
    PlannerView,
    TrackerUnavailable,
)
from opspilot.agent.store import Event, PostgresAgentStore, RunBundle
from opspilot.config import Settings
from opspilot.domain import AppError, ProviderError
from opspilot.observability import run_id as run_context
from opspilot.observability import span
from opspilot.retrieval import Retriever

MAX_INVALID_OUTPUTS = 2
EXCERPT = 400
OBSERVATION_WINDOW = 8


class AgentService:
    def __init__(
        self,
        store: PostgresAgentStore,
        retriever: Retriever,
        planner: Planner,
        tracker: IssueTracker | None,
        settings: Settings,
    ) -> None:
        self.store = store
        self.retriever = retriever
        self.planner = planner
        self.tracker = tracker
        self.settings = settings
        self.graph = build_graph(self)

    # ---------------------------------------------------------------- API use cases
    async def start(self, principal: Principal, request: str) -> RunBundle:
        authorize_start(principal)
        state = AgentState(
            run_id=str(uuid4()),
            tenant_id=str(principal.tenant),
            subject=principal.subject,
            request=request,
            status="planning",
            steps=0,
            invalid_outputs=0,
            observations=[],
            context=[],
            decision=None,
            answer=None,
            cited_chunk_ids=[],
            error=None,
        )
        token = run_context.set(state["run_id"])
        try:
            with span("agent.run"):
                await self.store.create_run(
                    state, [("run.created", principal.subject, {"request_chars": len(request)})]
                )
                await self.drive(state)
        finally:
            run_context.reset(token)
        return await self.bundle(principal.tenant, UUID(state["run_id"]))

    async def get(self, principal: Principal, run_id: UUID) -> RunBundle:
        return await self.bundle(principal.tenant, run_id)

    async def decide(
        self, principal: Principal, run_id: UUID, action_hash: str, approve: bool
    ) -> RunBundle:
        bundle = await self.bundle(principal.tenant, run_id)
        authorize_decision(principal, bundle.run.tenant_id, bundle.run.requested_by)
        decision: Literal["approved", "rejected"] = "approved" if approve else "rejected"
        token = run_context.set(str(run_id))
        try:
            with span("agent.approval"):
                await self.store.decide(
                    principal.tenant,
                    run_id,
                    principal.subject,
                    decision,
                    action_hash,
                    [(f"approval.{decision}", principal.subject, {"action_hash": action_hash})],
                )
            if approve:
                await self.resume_execution(principal.tenant, run_id)
        finally:
            run_context.reset(token)
        return await self.bundle(principal.tenant, run_id)

    async def resume(self, principal: Principal, run_id: UUID) -> RunBundle:
        """Continue after a crash/restart: planning, pending or ambiguous executions."""
        if not principal.roles & {"agent", "approver"}:
            raise Denied("role_required")
        bundle = await self.bundle(principal.tenant, run_id)
        token = run_context.set(str(run_id))
        try:
            if bundle.run.status == "planning":
                await self.drive(bundle.run.state)
            elif bundle.run.status in ("approved", "executing", "ambiguous"):
                await self.resume_execution(principal.tenant, run_id)
        finally:
            run_context.reset(token)
        return await self.bundle(principal.tenant, run_id)

    async def bundle(self, tenant: UUID, run_id: UUID) -> RunBundle:
        bundle = await self.store.load(tenant, run_id)
        if bundle is None:
            raise AgentError("not_found", 404)
        return bundle

    # ---------------------------------------------------------------- graph driving
    async def drive(self, state: AgentState) -> None:
        """Run planning until a durable stop: proposal, answer or terminal failure."""
        limit = self.settings.agent_max_steps * 2 + 4
        try:
            async with asyncio.timeout(self.settings.agent_deadline_seconds):
                await self.graph.ainvoke(state, {"recursion_limit": limit})
        except TimeoutError:
            await self.fail_planning(state, "deadline_exceeded")
        except GraphRecursionError:
            await self.fail_planning(state, "step_limit")

    async def fail_planning(self, state: AgentState, code: str) -> None:
        latest = await self.store.load(UUID(state["tenant_id"]), UUID(state["run_id"]))
        current = latest.run.state if latest else state
        failed = cast(AgentState, {**current, "status": "failed", "error": code})
        await self.store.save_planning(failed, [("run.failed", "system", {"code": code})])

    async def resume_execution(self, tenant: UUID, run_id: UUID) -> None:
        bundle = await self.bundle(tenant, run_id)
        state = cast(AgentState, {**bundle.run.state, "status": bundle.run.status})
        await self.graph.ainvoke(state, {"recursion_limit": 4})

    # ---------------------------------------------------------------- execution
    async def execute(self, tenant: UUID, run_id: UUID) -> None:
        bundle = await self.bundle(tenant, run_id)
        proposal, approval, execution = bundle.proposal, bundle.approval, bundle.execution
        if proposal is None or approval is None or execution is None:
            return
        current = proposal.action.action_hash()
        # The approval binds one exact action: recompute from what is stored right now.
        if not (
            approval.decision == "approved"
            and approval.action_hash == current
            and proposal.action_hash == current
            and execution.action_hash == current
        ):
            event: Event = (
                "approval.invalid",
                "system",
                {"approved_hash": approval.action_hash, "current_hash": current},
            )
            await self.store.fail_run(tenant, run_id, "approval_invalid", [event])
            return
        try:
            authorize_execution(self.settings.agent_policy, proposal.action)
        except Denied as denied:
            await self.store.fail_run(
                tenant, run_id, denied.code, [("execution.denied", "system", {"code": denied.code})]
            )
            return
        if self.tracker is None:
            await self.store.fail_run(
                tenant,
                run_id,
                "gitlab_unconfigured",
                [("execution.denied", "system", {"code": "gitlab_unconfigured"})],
            )
            return
        owner = uuid4()
        mode = await self.store.claim(tenant, run_id, owner, self.settings.execution_lease_seconds)
        if mode is None:
            return  # terminal already, or another live owner holds the lease
        action, key = proposal.action, execution.idempotency_key
        with span("agent.tool"):
            if mode == "reconcile":
                try:
                    found = await self.tracker.find_by_marker(action.project_id, key)
                except TrackerUnavailable:
                    await self.finish(
                        tenant, run_id, owner, CreateOutcome("unknown", "reconcile_unavailable")
                    )
                    return
                if found:
                    events: list[Event] = [
                        ("execution.reconciled", "system", {"found": len(found)})
                    ]
                    if len(found) > 1:
                        events.append(
                            ("execution.duplicate_detected", "system", {"count": len(found)})
                        )
                    await self.store.finish(
                        tenant, run_id, owner, "succeeded", "succeeded", None, found[0], events
                    )
                    return
                latest = (await self.bundle(tenant, run_id)).execution
                since = latest.seconds_since_attempt if latest else None
                if since is not None and since < self.settings.reconcile_grace_seconds:
                    # Too early to conclude "not created": the original request may land late.
                    await self.finish(
                        tenant, run_id, owner, CreateOutcome("unknown", "awaiting_reconcile_grace")
                    )
                    return
            latest = (await self.bundle(tenant, run_id)).execution
            if latest is not None and latest.attempts >= self.settings.max_execution_attempts:
                await self.finish(
                    tenant, run_id, owner, CreateOutcome("rejected", "attempts_exhausted")
                )
                return
            if not await self.store.begin_attempt(tenant, run_id, owner):
                return
            outcome = await self.tracker.create_issue(
                action.project_id,
                action.title,
                action.description + marker_footer(key),
                action.labels,
                action.assignee_ids,
            )
            await self.finish(tenant, run_id, owner, outcome)

    async def finish(self, tenant: UUID, run_id: UUID, owner: UUID, outcome: CreateOutcome) -> None:
        data: dict[str, object] = {"outcome": outcome.kind, "code": outcome.code}
        if outcome.kind == "created" and outcome.issue is not None:
            data |= {"issue_iid": outcome.issue.iid, "issue_url": outcome.issue.web_url}
            await self.store.finish(
                tenant,
                run_id,
                owner,
                "succeeded",
                "succeeded",
                None,
                outcome.issue,
                [("execution.succeeded", "system", data)],
            )
            return
        bundle = await self.bundle(tenant, run_id)
        attempts = bundle.execution.attempts if bundle.execution else 0
        exhausted = attempts >= self.settings.max_execution_attempts
        if outcome.kind == "unknown":
            # Never treat "unknown" as "failed": reconcile by marker before any resend.
            await self.store.finish(
                tenant,
                run_id,
                owner,
                "ambiguous",
                "ambiguous",
                outcome.code,
                None,
                [("execution.ambiguous", "system", data)],
            )
            return
        if (outcome.kind == "not_sent" or outcome.retryable) and not exhausted:
            await self.store.finish(
                tenant,
                run_id,
                owner,
                "pending",
                "approved",
                outcome.code,
                None,
                [("execution.retryable", "system", data)],
            )
            return
        await self.store.finish(
            tenant,
            run_id,
            owner,
            "failed_terminal",
            "failed",
            outcome.code,
            None,
            [("execution.failed", "system", data)],
        )


def _project_views(service: AgentService, tenant: UUID) -> list[dict[str, object]]:
    return [
        {"project": alias, "labels": sorted(project.labels), "assignees": sorted(project.assignees)}
        for alias, project in sorted(service.settings.agent_policy.projects(tenant).items())
    ]


def build_graph(
    service: AgentService,
) -> CompiledStateGraph[AgentState, None, AgentState, AgentState]:
    settings, store = service.settings, service.store

    async def commit(
        state: AgentState, updates: dict[str, object], events: list[Event]
    ) -> dict[str, object]:
        merged = cast(AgentState, {**state, **updates})
        if not await store.save_planning(merged, events):
            # The run left planning elsewhere (e.g. concurrent resume): stop this branch.
            return {"status": "failed", "error": "superseded"}
        return updates

    async def invalid(state: AgentState, reason: str, tool: object) -> dict[str, object]:
        count = state["invalid_outputs"] + 1
        observation = {"tool": tool if isinstance(tool, str) else None, "error": reason}
        updates: dict[str, object] = {
            "invalid_outputs": count,
            "steps": state["steps"] + 1,
            "observations": [*state["observations"], observation][-OBSERVATION_WINDOW:],
            "decision": None,
        }
        kind = "tool.denied" if reason == "tool_not_permitted" else "decision.invalid"
        events: list[Event] = [(kind, "model", {"reason": reason, "tool": str(tool)[:64]})]
        if count > MAX_INVALID_OUTPUTS:
            updates |= {"status": "failed", "error": "invalid_model_output"}
            events.append(("run.failed", "system", {"code": "invalid_model_output"}))
        return await commit(state, updates, events)

    async def plan(state: AgentState) -> dict[str, object]:
        if state["steps"] >= settings.agent_max_steps:
            return await commit(
                state,
                {"status": "failed", "error": "step_limit"},
                [("run.failed", "system", {"code": "step_limit"})],
            )
        tenant = UUID(state["tenant_id"])
        view = PlannerView(
            request=state["request"],
            projects=_project_views(service, tenant),
            observations=state["observations"][-OBSERVATION_WINDOW:],
            steps_remaining=settings.agent_max_steps - state["steps"],
        )
        try:
            with span("agent.llm"):
                async with asyncio.timeout(settings.agent_llm_timeout_seconds):
                    raw = await service.planner.decide(view)
        except (TimeoutError, ProviderError):
            return await commit(
                state,
                {"status": "failed", "error": "llm_unavailable"},
                [("run.failed", "system", {"code": "llm_unavailable"})],
            )
        tool = raw.get("tool") if isinstance(raw, dict) else None
        if not isinstance(tool, str):
            return await invalid(state, "malformed_decision", tool)
        try:
            authorize_tool(tool)
        except Denied:
            return await invalid(state, "tool_not_permitted", tool)
        try:
            decision = DECISION.validate_python(raw)
        except ValidationError:
            return await invalid(state, "invalid_arguments", tool)
        summary: dict[str, object] = {"tool": decision.tool}
        if isinstance(decision, SearchKnowledge):
            summary["query"] = decision.query
        elif isinstance(decision, PrepareGitLabIssue):
            summary |= {
                "project": decision.project,
                "title": decision.title,
                "labels": decision.labels,
                "assignees": decision.assignees,
                "description_chars": len(decision.description),
            }
        return await commit(
            state,
            {"decision": decision.model_dump(mode="json")},
            [("llm.decision", "model", summary)],
        )

    async def search_knowledge(state: AgentState) -> dict[str, object]:
        decision = SearchKnowledge.model_validate(state["decision"])
        tenant = UUID(state["tenant_id"])
        try:
            with span("agent.tool"):
                hits = await service.retriever.search(tenant, decision.query, 5)
        except AppError:
            return await commit(
                state,
                {"status": "failed", "error": "retrieval_unavailable"},
                [("run.failed", "system", {"code": "retrieval_unavailable"})],
            )
        refs = [
            {
                "chunk_id": str(hit.chunk.id),
                "document_id": str(hit.chunk.document_id),
                "title": hit.chunk.title,
            }
            for hit in hits
        ]
        observation: dict[str, object] = {
            "tool": "search_knowledge",
            "query": decision.query,
            # Untrusted evidence for the model; bounded excerpts, never full documents.
            "hits": [
                ref | {"excerpt": hit.chunk.text[:EXCERPT]}
                for ref, hit in zip(refs, hits, strict=True)
            ],
        }
        known = {item["chunk_id"] for item in state["context"]}
        context = [*state["context"], *(ref for ref in refs if ref["chunk_id"] not in known)]
        return await commit(
            state,
            {
                "steps": state["steps"] + 1,
                "observations": [*state["observations"], observation][-OBSERVATION_WINDOW:],
                "context": context,
                "decision": None,
            },
            [("tool.search_knowledge", "system", {"chunk_ids": [ref["chunk_id"] for ref in refs]})],
        )

    async def prepare_gitlab_issue(state: AgentState) -> dict[str, object]:
        request = PrepareGitLabIssue.model_validate(state["decision"])
        principal = Principal(tenant=UUID(state["tenant_id"]), subject=state["subject"])
        steps = state["steps"] + 1
        try:
            # Pure: resolves aliases from configuration and checks policy; no side effect.
            action = authorize_issue(settings.agent_policy, principal, request)
        except Denied as denied:
            observation = {"tool": "prepare_gitlab_issue", "error": denied.code}
            return await commit(
                state,
                {
                    "steps": steps,
                    "observations": [*state["observations"], observation][-OBSERVATION_WINDOW:],
                    "decision": None,
                },
                [("policy.denied", "system", {"code": denied.code, "project": request.project})],
            )
        updated = cast(
            AgentState, {**state, "steps": steps, "decision": None, "status": "awaiting_approval"}
        )
        stored = await store.store_proposal(
            updated,
            action,
            [
                (
                    "proposal.stored",
                    "system",
                    {
                        "action_hash": action.action_hash(),
                        "project": action.project,
                        "project_id": action.project_id,
                    },
                )
            ],
        )
        if not stored:
            return {"status": "failed", "error": "superseded"}
        return {"steps": steps, "decision": None, "status": "awaiting_approval"}

    async def final_answer(state: AgentState) -> dict[str, object]:
        decision = FinalAnswer.model_validate(state["decision"])
        known = {item["chunk_id"] for item in state["context"]}
        cited = [str(item) for item in decision.cited_chunk_ids]
        if any(item not in known for item in cited):
            return await invalid(state, "invalid_citation", "final_answer")
        return await commit(
            state,
            {
                "status": "answered",
                "answer": decision.answer,
                "cited_chunk_ids": cited,
                "decision": None,
            },
            [("run.answered", "model", {"cited": len(cited)})],
        )

    async def execute(state: AgentState) -> dict[str, object]:
        await service.execute(UUID(state["tenant_id"]), UUID(state["run_id"]))
        return {}

    def route(state: AgentState) -> str:
        if state["status"] == "planning":
            return "plan"
        if state["status"] in ("approved", "executing", "ambiguous"):
            return "execute"
        return END

    def after_plan(state: AgentState) -> str:
        if state["status"] != "planning" or state["decision"] is None:
            return "plan" if state["status"] == "planning" else END
        return str(state["decision"]["tool"])

    def after_tool(state: AgentState) -> str:
        return "plan" if state["status"] == "planning" else END

    graph: StateGraph[AgentState, None, AgentState, AgentState] = StateGraph(AgentState)
    graph.add_node("plan", plan)
    graph.add_node("search_knowledge", search_knowledge)
    graph.add_node("prepare_gitlab_issue", prepare_gitlab_issue)
    graph.add_node("final_answer", final_answer)
    graph.add_node("execute", execute)
    graph.add_conditional_edges(START, route, ["plan", "execute", END])
    graph.add_conditional_edges(
        "plan",
        after_plan,
        ["plan", "search_knowledge", "prepare_gitlab_issue", "final_answer", END],
    )
    for node in ("search_knowledge", "prepare_gitlab_issue", "final_answer"):
        graph.add_conditional_edges(node, after_tool, ["plan", END])
    graph.add_edge("execute", END)
    return graph.compile()
