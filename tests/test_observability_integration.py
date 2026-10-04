"""Traces, metrics and logs from real flows (PostgreSQL + fake GitLab over HTTP)."""

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
from openai import AsyncOpenAI
from opentelemetry.sdk.trace import ReadableSpan

from opspilot.agent.planners import OpenAIPlanner, ScriptedPlanner
from opspilot.api.app import create_app
from opspilot.application import RagService
from opspilot.config import Settings
from opspilot.observability import METRIC_LABELS, UUID_LIKE
from opspilot.persistence.postgres import PostgresRepository
from opspilot.providers.fake import FakeProvider
from scripts.fake_gitlab import serve_in_thread
from tests.agent_support import SEARCH, Env, prepare, service
from tests.telemetry_support import Telemetry, every_text

pytestmark = pytest.mark.integration
ISSUE = "Please open an issue to restart payments-api"


def trace_of(spans: list[ReadableSpan], trace_id: str) -> list[ReadableSpan]:
    return [
        s for s in spans if s.context is not None and format(s.context.trace_id, "032x") == trace_id
    ]


def named(spans: list[ReadableSpan], name: str) -> ReadableSpan:
    (found,) = [s for s in spans if s.name == name]
    return found


def attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


async def test_rag_request_trace_breakdown_and_propagation(
    postgres: tuple[PostgresRepository, UUID, UUID], settings: Settings, telemetry: Telemetry
) -> None:
    repository, a, _ = postgres
    fake = FakeProvider()
    config = Settings.model_validate(settings.model_dump() | {"tenant_tokens": {"t" * 40: str(a)}})
    app = create_app(config, RagService(repository, fake, fake))
    incoming = "4bf92f3577b34da6a3ce929d0e0e4736"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as http:
        auth = {"Authorization": f"Bearer {'t' * 40}"}
        await http.post(
            "/v1/documents",
            headers=auth,
            json={
                "content": "Restart payments-api: drain traffic first.",
                "metadata": {"title": "r"},
            },
        )
        telemetry.clear()
        response = await http.post(
            "/v1/query",
            headers=auth | {"traceparent": f"00-{incoming}-00f067aa0ba902b7-01"},
            json={"question": "How do I restart payments-api?"},
        )
    assert response.status_code == 200
    trace_id = response.headers["x-trace-id"]
    assert trace_id == incoming  # W3C context from the caller is continued
    spans = trace_of(telemetry.spans(), trace_id)
    names = {s.name for s in spans}
    assert {
        "http.request",
        "rag.query",
        "retrieval",
        "embedding",
        "vector_retrieval",
        "lexical_retrieval",
        "rank_fusion",
        "context_build",
        "llm.request",
    } <= names
    by_id = {s.context.span_id: s for s in spans if s.context}
    parent = {
        s.name: by_id[s.parent.span_id].name
        for s in spans
        if s.parent and s.parent.span_id in by_id
    }
    assert parent["rag.query"] == "http.request" and parent["retrieval"] == "rag.query"
    assert (
        parent["vector_retrieval"]
        == parent["lexical_retrieval"]
        == parent["rank_fusion"]
        == "retrieval"
    )
    assert parent["context_build"] == parent["llm.request"] == "rag.query"
    retrieval = attrs(named(spans, "retrieval"))
    assert retrieval["ai.retrieval.strategy"] == "hybrid" and retrieval["ai.retrieval.top_k"] == 5
    assert retrieval["ai.retrieval.candidates_requested"] == 20
    assert retrieval["ai.retrieval.chunks_returned"] >= 1
    http_span = attrs(named(spans, "http.request"))
    assert http_span["http.route"] == "/v1/query" and http_span["http.response.status_code"] == 200
    assert http_span["opspilot.request_id"] == response.headers["x-request-id"]
    llm = attrs(named(spans, "llm.request"))
    assert (llm["ai.provider"], llm["ai.operation"], llm["ai.usage.known"]) == (
        "fake",
        "answer",
        False,
    )
    exported = " ".join(every_text(spans))
    assert "drain traffic" not in exported and "How do I restart" not in exported


async def test_agent_run_is_reconstructable_from_traces_audit_and_logs(
    agent_env: Env, telemetry: Telemetry, caplog: pytest.LogCaptureFixture
) -> None:
    before = telemetry.metrics()
    with caplog.at_level(logging.INFO, logger="opspilot"):
        async with service(agent_env) as svc:
            proposed = await svc.start(agent_env.alice, ISSUE)
            assert proposed.proposal is not None
            agent_env.fault("drop_after_create")
            await svc.decide(agent_env.bob, proposed.run.id, proposed.proposal.action_hash, True)
            done = await svc.resume(agent_env.alice, proposed.run.id)
            events = await svc.store.events(agent_env.a, proposed.run.id)
    run = str(proposed.run.id)
    assert done.run.status == "succeeded"
    spans = [s for s in telemetry.spans() if attrs(s).get("opspilot.run_id") == run]
    names = [s.name for s in spans]
    for expected in (
        "agent.run",
        "agent.plan",
        "llm.request",
        "knowledge_search",
        "retrieval",
        "authorization",
        "approval",
        "tool.execute",
        "gitlab.request",
        "reconciliation",
    ):
        assert expected in names, expected
    entries = sorted(attrs(s)["ai.agent.entry"] for s in spans if s.name == "agent.run")
    assert entries == ["approve", "resume", "start"]
    policy = [attrs(s) for s in spans if s.name == "authorization"]
    assert {p["ai.operation"] for p in policy} >= {"start", "tool", "prepare", "decide", "execute"}
    assert all(p["ai.policy.result"] == "allow" for p in policy)
    outcomes = [attrs(s)["ai.execution.outcome"] for s in spans if s.name == "tool.execute"]
    assert outcomes == ["ambiguous", "succeeded"]
    assert attrs(named(spans, "reconciliation"))["ai.reconciliation.result"] == "found"
    # Every audit event carries the trace that produced it; all those traces belong to the run.
    run_traces = {format(s.context.trace_id, "032x") for s in spans if s.context}
    assert {event["data"]["trace_id"] for event in events} <= run_traces
    logged = [json.loads(r.getMessage()) for r in caplog.records if r.getMessage().startswith("{")]
    assert {line["trace_id"] for line in logged if line["run_id"] == run} <= run_traces
    assert any(line["operation"] == "tool.execute" for line in logged if line["run_id"] == run)
    assert telemetry.delta(before, "ambiguous_execution_total", reason="gitlab_response_lost") == 1
    assert telemetry.delta(before, "reconciliation_attempt_total") == 1
    assert telemetry.delta(before, "reconciliation_success_total") == 1
    assert telemetry.delta(before, "approval_requested_total") == 1
    assert telemetry.delta(before, "approval_approved_total") == 1
    assert telemetry.delta(before, "approval_duration_seconds", result="approved") == 1
    assert telemetry.delta(before, "agent_success_total", status="succeeded") == 1
    assert telemetry.delta(before, "tool_calls_total", tool="create_gitlab_issue") == 2
    assert (
        telemetry.delta(
            before, "tool_failures_total", tool="create_gitlab_issue", error_type="ambiguous"
        )
        == 1
    )


async def test_policy_and_model_failures_are_counted(agent_env: Env, telemetry: Telemetry) -> None:
    from opspilot.agent.ports import AgentError

    before = telemetry.metrics()
    script = [
        SEARCH,
        {"tool": "create_gitlab_issue", "project": "payments-ops"},
        prepare("secret-admin"),
        {"tool": "search_knowledge"},
        prepare(),
    ]
    async with service(agent_env, ScriptedPlanner(script)) as svc:
        bundle = await svc.start(agent_env.alice, ISSUE)
        assert bundle.proposal is not None
        with pytest.raises(AgentError):
            await svc.decide(agent_env.bob, bundle.run.id, "0" * 64, True)
    assert telemetry.delta(before, "policy_denied_total", reason="project_not_allowed") == 1
    assert telemetry.delta(before, "policy_denied_total", reason="tool_not_permitted") == 1
    assert telemetry.delta(before, "unauthorized_tool_total") == 1
    assert telemetry.delta(before, "invalid_model_output_total", reason="invalid_arguments") == 1
    assert telemetry.delta(before, "approval_hash_mismatch_total") == 1
    async with service(
        agent_env, ScriptedPlanner([SEARCH], repeat_last=True), agent_max_steps=2
    ) as svc:
        await svc.start(agent_env.alice, "loop")
    assert telemetry.delta(before, "agent_failure_total", reason="step_limit") == 1


async def test_metric_labels_stay_bounded_after_real_flows(
    agent_env: Env, telemetry: Telemetry
) -> None:
    async with service(agent_env) as svc:
        for _ in range(3):
            proposed = await svc.start(agent_env.alice, ISSUE)
            assert proposed.proposal is not None
            await svc.decide(agent_env.bob, proposed.run.id, proposed.proposal.action_hash, True)
        await svc.start(agent_env.alice, "How do I restart payments-api?")
        await svc.start(agent_env.mallory, "How do I restart payments-api?")
    identifiers = {str(agent_env.a), str(agent_env.b), "alice", "bob", "mallory"}
    combinations: dict[str, set[frozenset[tuple[str, str]]]] = {}
    for (name, labels), _ in telemetry.metrics().items():
        keys = {key for key, _ in labels}
        assert keys <= METRIC_LABELS, (name, keys)
        for _, value in labels:
            assert not UUID_LIKE.search(value) and value not in identifiers, (name, value)
        combinations.setdefault(name, set()).add(labels)
    assert max(len(values) for values in combinations.values()) <= 40


def plan_response(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "resp",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": "gpt-4.1-mini",
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 100,
            "total_tokens": 1100,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
        "output": [
            {
                "type": "message",
                "id": "m",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps({"decision": decision}),
                        "annotations": [],
                    }
                ],
            }
        ],
    }


async def test_cost_is_persisted_at_call_time_and_not_rewritten(agent_env: Env) -> None:
    decisions: list[dict[str, Any]] = [
        SEARCH,
        {"tool": "final_answer", "answer": "Done.", "cited_chunk_ids": []},
    ]

    def price(input_cost: str, since: datetime) -> dict[str, Any]:
        return {
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "input_cost_per_million": input_cost,
            "output_cost_per_million": "1.00",
            "effective_from": since.isoformat(),
        }

    async def run_with(prices: list[dict[str, Any]]) -> list[dict[str, Any]]:
        calls = iter(decisions)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=plan_response(next(calls)))

        settings = agent_env.settings(
            provider="openai",
            openai_api_key="mock-key",
            model_pricing={"prices": prices},
        )
        client = AsyncOpenAI(
            api_key="mock-key",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        planner = OpenAIPlanner(settings, client)
        async with service(agent_env, planner, model_pricing={"prices": prices}) as svc:
            bundle = await svc.start(agent_env.alice, "How do I restart payments-api?")
            return [
                e["data"]
                for e in await svc.store.events(agent_env.a, bundle.run.id)
                if e["type"] == "llm.decision"
            ]

    old = datetime(2026, 1, 1, tzinfo=UTC)
    first = await run_with([price("2.00", old)])
    # 1000 input tokens at 2.00/M + 100 output at 1.00/M = 0.0021 per call.
    assert [d["estimated_cost_usd"] for d in first] == ["0.0021", "0.0021"]
    assert first[0]["input_tokens"] == 1000 and first[0]["output_tokens"] == 100
    newer = datetime.now(UTC) - timedelta(seconds=1)
    second = await run_with([price("2.00", old), price("4.00", newer)])
    assert [d["estimated_cost_usd"] for d in second] == ["0.0041", "0.0041"]
    unpriced = await run_with([])
    assert [d["estimated_cost_usd"] for d in unpriced] == [None, None]
    assert first[0]["estimated_cost_usd"] == "0.0021"  # history unchanged: stored at call time
    assert Decimal(second[0]["estimated_cost_usd"]) > Decimal(first[0]["estimated_cost_usd"])


SENTINEL = "TEST_SECRET_DO_NOT_LOG_"


async def test_sentinel_secrets_never_reach_logs_traces_or_metrics(
    agent_env: Env,
    telemetry: Telemetry,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    requester = SENTINEL + "REQUESTER_TOKEN_0001"
    approver = SENTINEL + "APPROVER_TOKEN_00002"
    gitlab_token = SENTINEL + "GITLAB_TOKEN_0003"
    server, url = serve_in_thread(gitlab_token, {101})
    tokens = {
        requester: {"tenant": str(agent_env.a), "subject": "alice", "roles": ["agent"]},
        approver: {"tenant": str(agent_env.a), "subject": "bob", "roles": ["approver"]},
    }
    before = telemetry.metrics()
    try:
        with caplog.at_level(logging.DEBUG):
            async with service(agent_env, tracker_url=url) as agent:
                agent.tracker.client.headers["PRIVATE-TOKEN"] = gitlab_token  # type: ignore[union-attr]
                settings = Settings.model_validate(
                    agent.settings.model_dump() | {"tenant_tokens": tokens}
                )
                fake = FakeProvider()
                rag = RagService(agent.store.repository, fake, fake)
                app = create_app(settings, rag, agent)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://t"
                ) as http:
                    auth = {"Authorization": f"Bearer {requester}"}
                    document = f"Payments runbook {SENTINEL}DOCUMENT_0004 restart payments-api."
                    ok = await http.post(
                        "/v1/documents",
                        headers=auth,
                        json={"content": document, "metadata": {"title": "runbook"}},
                    )
                    assert ok.status_code == 201
                    question = f"restart payments-api {SENTINEL}QUESTION_0005"
                    assert (
                        await http.post("/v1/query", headers=auth, json={"question": question})
                    ).status_code == 200
                    bad = await http.post(
                        "/v1/query",
                        headers={"Authorization": f"Bearer {SENTINEL}INVALID_TOKEN_0006"},
                        json={"question": "x"},
                    )
                    assert bad.status_code == 401
                    run = await http.post(
                        "/v1/agent/runs",
                        headers=auth,
                        json={
                            "request": f"Please open an issue {SENTINEL}PROMPT_0007 for payments"
                        },
                    )
                    assert run.status_code == 201
                    body = run.json()
                    approved = await http.post(
                        f"/v1/agent/runs/{body['run_id']}/approve",
                        headers={"Authorization": f"Bearer {approver}"},
                        json={"action_hash": body["proposal"]["action_hash"]},
                    )
                    assert approved.status_code == 200
                    assert approved.json()["status"] == "succeeded"
    finally:
        server.shutdown()
        server.server_close()
    captured = capfd.readouterr()
    haystacks = {
        "log records": "\n".join(r.getMessage() for r in caplog.records),
        "stdout": captured.out,
        "stderr": captured.err,
        "spans": "\n".join(every_text(telemetry.spans())),
        "metrics": str({k: v for k, v in telemetry.metrics().items() if k not in before}),
    }
    assert caplog.records and telemetry.spans()  # the capture really saw the flows
    for where, text in haystacks.items():
        assert SENTINEL not in text, where
