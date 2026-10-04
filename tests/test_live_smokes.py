"""The opt-in live smokes fail closed, and their logic is rehearsed offline.

Rehearsals prove the scripts' own logic only (mock OpenAI transport, fake GitLab server);
they are not live evidence and are reported separately from it.
"""

import asyncio
import io
import json
import logging
import os
import socket
import sys
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from opspilot.agent.gitlab import GitLabTracker
from opspilot.config import Settings
from opspilot.observability import logger
from scripts import live_gitlab_smoke, live_openai_smoke
from scripts.fake_gitlab import serve_in_thread

SENTINEL_KEY = "sk-TEST_SECRET-live-smoke-rehearsal-key-000000"
LIVE_ENV = (
    "OPENAI_API_KEY",
    "OPSPILOT_ALLOW_REAL_OPENAI_SMOKE",
    "GITLAB_BASE_URL",
    "GITLAB_TOKEN",
    "GITLAB_PROJECT_ID",
    "OPSPILOT_ALLOW_REAL_GITLAB_SMOKE",
)


@pytest.mark.parametrize(
    ("environ", "reason"),
    [
        ({}, "OPENAI_API_KEY not provided"),
        ({"OPENAI_API_KEY": SENTINEL_KEY}, "OPSPILOT_ALLOW_REAL_OPENAI_SMOKE=true"),
        ({"OPENAI_API_KEY": SENTINEL_KEY, "OPSPILOT_ALLOW_REAL_OPENAI_SMOKE": "1"}, "=true"),
    ],
)
def test_openai_smoke_gate(environ: dict[str, str], reason: str) -> None:
    refusal = live_openai_smoke.gate_failure(environ)
    assert refusal is not None and refusal.startswith("NOT EXECUTED") and reason in refusal
    assert SENTINEL_KEY not in refusal


COMPLETE = {
    "GITLAB_BASE_URL": "https://gitlab.example.com",
    "GITLAB_TOKEN": "glpat-TEST_SECRET-rehearsal",
    "GITLAB_PROJECT_ID": "42",
    "DATABASE_URL": "postgresql+asyncpg://u:p@db/opspilot",
    "OPSPILOT_ALLOW_REAL_GITLAB_SMOKE": "true",
}


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"GITLAB_TOKEN": ""}, "missing GITLAB_TOKEN"),
        ({"GITLAB_PROJECT_ID": ""}, "missing GITLAB_PROJECT_ID"),
        ({"GITLAB_BASE_URL": ""}, "missing GITLAB_BASE_URL"),
        ({"DATABASE_URL": ""}, "missing DATABASE_URL"),
        ({"OPSPILOT_ALLOW_REAL_GITLAB_SMOKE": "yes"}, "=true"),
        ({"GITLAB_BASE_URL": "http://gitlab.example.com"}, "https"),
        ({"GITLAB_PROJECT_ID": "group/project"}, "numeric"),
    ],
)
def test_gitlab_smoke_fails_closed(override: dict[str, str], reason: str) -> None:
    refusal = live_gitlab_smoke.gate_failure(COMPLETE | override)
    assert refusal is not None and refusal.startswith("NOT EXECUTED") and reason in refusal
    assert COMPLETE["GITLAB_TOKEN"] not in refusal
    assert live_gitlab_smoke.gate_failure(COMPLETE) is None


@pytest.mark.parametrize("module", [live_openai_smoke, live_gitlab_smoke])
def test_default_invocation_touches_no_network(
    module: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in LIVE_ENV:
        monkeypatch.delenv(name, raising=False)

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(sys, "argv", ["smoke"])
    with pytest.raises(SystemExit) as stopped:
        module.main()
    assert stopped.value.code == 2
    assert capsys.readouterr().out.startswith("NOT EXECUTED")


def response(model: str, payload: object) -> dict[str, object]:
    return {
        "id": "resp_rehearsal",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": model,
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 40,
            "output_tokens": 12,
            "total_tokens": 52,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
        "output": [
            {
                "type": "message",
                "id": "msg_rehearsal",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": json.dumps(payload), "annotations": []}
                ],
            }
        ],
    }


async def openai_response(request: httpx.Request) -> httpx.Response:
    assert request.headers["authorization"] == f"Bearer {SENTINEL_KEY}"
    path = request.url.path
    if path.startswith("/v1/models/"):
        return httpx.Response(200, json={"id": path.rsplit("/", 1)[1], "object": "model"})
    body = json.loads(request.content)
    if path == "/v1/embeddings":
        if body["input"] == ["timeout probe"]:
            await asyncio.sleep(0.2)
        assert body["dimensions"] == 256
        data = [
            {"object": "embedding", "index": i, "embedding": [0.5] * 256}
            for i in range(len(body["input"]))
        ]
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": data,
                "model": "text-embedding-3-small",
                "usage": {"prompt_tokens": 8, "total_tokens": 8},
            },
        )
    assert body["text"]["format"]["strict"] is True
    name = body["text"]["format"]["name"]
    if name == "Envelope":
        decision = {"tool": "search_knowledge", "query": "restart payments-api"}
        return httpx.Response(200, json=response("gpt-4.1-mini-2025-04-14", {"decision": decision}))
    return httpx.Response(
        200, json=response("gpt-4.1-mini-2025-04-14", {"answer": "Drain", "cited_chunk_ids": []})
    )


async def test_openai_smoke_rehearsal_on_mock_transport() -> None:
    served = "gpt-4.1-mini-2025-04-14"
    client = AsyncOpenAI(
        api_key=SENTINEL_KEY,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(openai_response)),
    )
    settings = live_openai_smoke.settings_from({"OPENAI_API_KEY": SENTINEL_KEY})
    outcome = await live_openai_smoke.run(settings, client)
    steps = {step["step"]: step for step in outcome["steps"]}
    assert outcome["passed"] is True and outcome["secret_in_logs_or_spans"] is False
    assert steps["embedding"]["dimensions"] == 256
    assert steps["structured_generation"]["schema_accepted"] is True
    assert steps["planner_structured_output"]["tool"] == "search_knowledge"
    assert steps["rag_end_to_end"]["status"] == "skipped_no_database"
    assert steps["timeout"]["timed_out"] is True
    assert outcome["requests_reserved"] <= live_openai_smoke.MAX_REQUESTS
    answers = [u for u in outcome["usage"] if u["operation"] in ("answer", "plan")]
    assert answers and all(u["served_model"] == served for u in answers)
    assert all(u["trace_id_present"] for u in outcome["usage"])
    assert SENTINEL_KEY not in json.dumps(outcome)
    assert isinstance(settings, Settings)


async def test_openai_smoke_does_not_skip_a_database_containing_unused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AsyncOpenAI(
        api_key=SENTINEL_KEY,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(openai_response)),
    )
    settings = live_openai_smoke.settings_from(
        {
            "OPENAI_API_KEY": SENTINEL_KEY,
            "DATABASE_URL": "postgresql+asyncpg://u:unused_password@db/opspilot",
        }
    )

    def unavailable(config: Settings) -> None:
        raise RuntimeError("configured database was reached")

    monkeypatch.setattr(live_openai_smoke, "PostgresRepository", unavailable)
    try:
        with pytest.raises(RuntimeError, match="configured database was reached"):
            await live_openai_smoke.run(settings, client)
    finally:
        await client.close()


@pytest.mark.parametrize(
    "body",
    [
        {"project_id": 101, "iid": 1, "description": "copied marker with other content"},
        {"project_id": 102, "iid": 1, "description": "approved description"},
        {"project_id": 101, "iid": 2, "description": "approved description"},
    ],
)
def test_gitlab_smoke_rejects_unrelated_cleanup_candidates(body: dict[str, Any]) -> None:
    assert not live_gitlab_smoke.owns_issue(body, 101, 1, "approved description")


@pytest.mark.parametrize(
    "field",
    ["input_tokens", "output_tokens", "served_model", "estimated_cost_usd", "trace_id_present"],
)
def test_openai_smoke_requires_the_evidence_it_claims(field: str) -> None:
    steps: list[dict[str, Any]] = [
        {"step": "structured_generation", "citations_within_evidence": True},
        {"step": "rag_end_to_end", "status": "skipped_no_database"},
        {"step": "timeout", "timed_out": True, "elapsed_ms": 1},
    ]
    usage = [
        {
            "operation": operation,
            "input_tokens": 8,
            "output_tokens": 0,
            "served_model": "served-model",
            "estimated_cost_usd": "0.0001",
            "trace_id_present": True,
            "error_type": None,
        }
        for operation in ("embedding", "answer", "plan")
    ] + [{"error_type": "timeout", "trace_id_present": True}]
    assert all(live_openai_smoke.evidence_checks(steps, usage, True).values())
    usage[0][field] = None
    assert not all(live_openai_smoke.evidence_checks(steps, usage, True).values())


def test_openai_smoke_does_not_treat_every_provider_error_as_timeout() -> None:
    checks = live_openai_smoke.evidence_checks(
        [{"step": "timeout", "timed_out": True, "elapsed_ms": 1}],
        [{"error_type": "provider_4xx", "trace_id_present": True}],
        False,
    )
    assert checks["timeout_classified_and_bounded"] is False


async def test_gitlab_smoke_counts_a_request_when_its_response_is_lost() -> None:
    def lost(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("synthetic response loss")

    transport = live_gitlab_smoke.CountingTransport()
    await transport.inner.aclose()
    transport.inner = httpx.MockTransport(lost)
    try:
        with pytest.raises(httpx.ReadError):
            await transport.handle_async_request(
                httpx.Request("POST", "https://gitlab.example.com")
            )
    finally:
        await transport.aclose()
    assert transport.sent == [("POST", None)]


@pytest.mark.parametrize("module", [live_openai_smoke, live_gitlab_smoke])
def test_live_smoke_cli_never_echoes_failure_messages(
    module: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "TEST_SECRET-do-not-echo-smoke-exception"
    for name, value in (
        COMPLETE
        | {
            "OPENAI_API_KEY": secret,
            "GITLAB_TOKEN": secret,
            "OPSPILOT_ALLOW_REAL_OPENAI_SMOKE": "true",
        }
    ).items():
        monkeypatch.setenv(name, value)

    async def failing(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise ValueError(secret)

    monkeypatch.setattr(module, "run", failing)
    monkeypatch.setattr(sys, "argv", ["smoke"])
    with pytest.raises(SystemExit) as stopped:
        module.main()
    assert stopped.value.code == 1
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert json.loads(captured.out)["failure_type"] == "ValueError"


@pytest.mark.parametrize(
    "modules",
    [(live_openai_smoke, live_gitlab_smoke), (live_gitlab_smoke, live_openai_smoke)],
)
def test_cli_logging_survives_closed_streams_and_restores_handlers(
    modules: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in (
        COMPLETE | {"OPENAI_API_KEY": SENTINEL_KEY, "OPSPILOT_ALLOW_REAL_OPENAI_SMOKE": "true"}
    ).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "argv", ["smoke"])
    closed_stream = io.StringIO()
    closed_stream.close()
    original = logging.StreamHandler(closed_stream)
    monkeypatch.setattr(logger, "handlers", [original])

    async def failing(*args: Any, **kwargs: Any) -> dict[str, Any]:
        logger.info("safe-cli-event")
        raise ValueError("TEST_SECRET-exception")

    for module in (*modules, *modules):
        stdout, stderr = io.StringIO(), io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout)
        monkeypatch.setattr(sys, "stderr", stderr)
        monkeypatch.setattr(module, "run", failing)
        with pytest.raises(SystemExit) as stopped:
            module.main()
        assert stopped.value.code == 1
        assert json.loads(stdout.getvalue())["failure_type"] == "ValueError"
        assert stderr.getvalue() == "safe-cli-event\n"
        assert logger.handlers == [original]
        assert original.stream is closed_stream
        assert not stderr.closed
        stderr.close()  # The next CLI must not flush or retain this capture.


@pytest.mark.integration
async def test_gitlab_smoke_rehearsal_against_fake_gitlab() -> None:
    token = "fake-gitlab-TEST_SECRET-token"
    server, url = serve_in_thread(token, {101})
    try:
        outcome = await live_gitlab_smoke.run(
            url, token, 101, os.environ["TEST_DATABASE_URL"], allow_http=True
        )
    finally:
        server.shutdown()
    assert outcome["passed"] is True, outcome
    issues = server.state.issues
    assert len(issues) == 1 and issues[0]["state"] == "closed"
    assert outcome["marker"] in issues[0]["description"]
    assert outcome["issue"]["final_state"] == "closed"
    rerun = next(c for c in outcome["checks"] if c["step"] == "idempotency_rerun")
    assert rerun["create_requests_sent"] == 1 and rerun["issues_with_marker"] == 1
    assert rerun["second_approval_http"] == 409
    encoded = json.dumps(outcome)
    assert token not in encoded and url not in encoded and "web_url" not in encoded


@pytest.mark.integration
async def test_gitlab_smoke_closes_issue_after_a_lost_create_response() -> None:
    token = "fake-gitlab-TEST_SECRET-cleanup-token"
    server, url = serve_in_thread(token, {101})
    server.state.faults = ["drop_after_create"]
    try:
        outcome = await live_gitlab_smoke.run(
            url, token, 101, os.environ["TEST_DATABASE_URL"], allow_http=True
        )
    finally:
        server.shutdown()
        server.server_close()
    assert outcome["passed"] is False
    assert len(server.state.issues) == 1 and server.state.issues[0]["state"] == "closed"
    assert any(c["step"] == "cleanup_close" and c["http"] == 200 for c in outcome["checks"])
    assert outcome["adapter_requests"][-1] == {"method": "GET", "http": 200}


@pytest.mark.integration
async def test_gitlab_smoke_cleanup_preserves_an_issue_with_a_copied_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "fake-gitlab-TEST_SECRET-cleanup-collision"
    server, url = serve_in_thread(token, {101})
    server.state.faults = ["drop_after_create"]
    original = GitLabTracker.find_by_marker

    async def lookup(self: Any, project: int, marker: str) -> Any:
        with server.state.lock:
            assert len(server.state.issues) == 1
            server.state.issues.append(
                server.state.issues[0]
                | {"id": 1002, "iid": 2, "description": f"Unrelated issue quoting {marker}"}
            )
        return await original(self, project, marker)

    monkeypatch.setattr(GitLabTracker, "find_by_marker", lookup)
    try:
        outcome = await live_gitlab_smoke.run(
            url, token, 101, os.environ["TEST_DATABASE_URL"], allow_http=True
        )
    finally:
        server.shutdown()
        server.server_close()
    assert outcome["passed"] is False
    assert [issue["state"] for issue in server.state.issues] == ["closed", "opened"]
    assert any(c["step"] == "cleanup_refused" for c in outcome["checks"])
