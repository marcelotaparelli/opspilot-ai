import json
import logging
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr

from opspilot.agent.gitlab import GitLabTracker
from opspilot.agent.planners import OpenAIPlanner
from opspilot.agent.ports import PlannerView, TrackerUnavailable
from opspilot.config import Settings
from opspilot.domain import ProviderError
from tests.helpers import TENANT_A, TOKEN_A

TOKEN = "glpat-SECRET-TOKEN-VALUE"
CREATED = {"id": 55, "iid": 3, "web_url": "https://gitlab.example.com/g/p/-/issues/3"}


def tracker(handler: Any) -> GitLabTracker:
    return GitLabTracker(
        "https://gitlab.example.com", TOKEN, 1.0, transport=httpx.MockTransport(handler)
    )


async def create(gitlab: GitLabTracker) -> Any:
    try:
        return await gitlab.create_issue(101, "Title", "Body <!-- m -->", ("ops", "incident"), (7,))
    finally:
        await gitlab.close()


async def test_create_request_shape_and_success() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json=CREATED | {"description": "Body"})

    outcome = await create(tracker(handler))
    assert outcome.kind == "created" and outcome.issue.iid == 3
    request = seen[0]
    assert str(request.url) == "https://gitlab.example.com/api/v4/projects/101/issues"
    assert request.headers["PRIVATE-TOKEN"] == TOKEN
    assert json.loads(request.content) == {
        "title": "Title",
        "description": "Body <!-- m -->",
        "labels": "ops,incident",
        "assignee_ids": [7],
    }


@pytest.mark.parametrize(
    ("status", "kind", "code", "retryable"),
    [
        (400, "rejected", "gitlab_bad_request", False),
        (401, "rejected", "gitlab_unauthorized", False),
        (403, "rejected", "gitlab_forbidden", False),
        (404, "rejected", "gitlab_not_found", False),
        (409, "rejected", "gitlab_conflict", False),
        (422, "rejected", "gitlab_unprocessable", False),
        (429, "rejected", "gitlab_rate_limited", True),
        (302, "rejected", "gitlab_rejected", False),
        (500, "unknown", "gitlab_server_error", False),
        (502, "unknown", "gitlab_server_error", False),
        (503, "unknown", "gitlab_server_error", False),
        (504, "unknown", "gitlab_server_error", False),
    ],
)
async def test_status_classification(status: int, kind: str, code: str, retryable: bool) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        headers = {"Location": "https://evil.example.com/steal"} if status == 302 else {}
        return httpx.Response(status, json={"message": "SECRET_DETAIL"}, headers=headers)

    outcome = await create(tracker(handler))
    assert (outcome.kind, outcome.code, outcome.retryable) == (kind, code, retryable)
    assert calls == 1  # no retries and no redirect following (token never leaves the host)


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (httpx.ConnectError("refused"), "not_sent"),
        (httpx.ConnectTimeout("slow connect"), "not_sent"),
        (httpx.PoolTimeout("pool"), "not_sent"),
        (httpx.ReadTimeout("response lost"), "unknown"),
        (httpx.WriteTimeout("partial write"), "unknown"),
        (httpx.RemoteProtocolError("closed"), "unknown"),
    ],
)
async def test_transport_failures_distinguish_not_sent_from_unknown(
    error: Exception, kind: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    outcome = await create(tracker(handler))
    assert outcome.kind == kind
    assert outcome.retryable is (kind == "not_sent")


async def test_malformed_success_body_is_unknown_not_failure() -> None:
    outcome = await create(tracker(lambda request: httpx.Response(201, json={"ok": True})))
    assert (outcome.kind, outcome.code) == ("unknown", "gitlab_malformed_response")


async def test_marker_lookup_requires_exact_marker() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["search"] == "opspilot-abc"
        assert request.url.params["in"] == "description"
        return httpx.Response(
            200,
            json=[
                CREATED | {"description": "x <!-- opspilot-action: opspilot-abc -->"},
                {"id": 2, "iid": 4, "web_url": "u", "description": "fuzzy match only"},
            ],
        )

    gitlab = tracker(handler)
    try:
        found = await gitlab.find_by_marker(101, "opspilot-abc")
    finally:
        await gitlab.close()
    assert [issue.iid for issue in found] == [3]


@pytest.mark.parametrize(
    "response",
    [httpx.Response(500), httpx.Response(200, json={"not": "a list"}), httpx.Response(401)],
)
async def test_marker_lookup_failures_are_unavailable(response: httpx.Response) -> None:
    gitlab = tracker(lambda request: response)
    try:
        with pytest.raises(TrackerUnavailable):
            await gitlab.find_by_marker(101, "m")
    finally:
        await gitlab.close()


async def test_token_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        await create(tracker(lambda request: httpx.Response(401)))
        await create(tracker(lambda request: httpx.Response(201, json=CREATED)))
    assert TOKEN not in caplog.text
    assert '"operation":"gitlab.request"' in caplog.text


def planner(handler: Any) -> OpenAIPlanner:
    settings = Settings.model_validate(
        {
            "database_url": SecretStr("postgresql+asyncpg://u:p@h/d"),
            "tenant_tokens": {TOKEN_A: str(TENANT_A)},
            "provider": "openai",
            "openai_api_key": SecretStr("mock-key"),
            "agent_llm_timeout_seconds": 1.0,
        }
    )
    client = AsyncOpenAI(
        api_key="mock-key",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return OpenAIPlanner(settings, client)


def response(text: str) -> dict[str, Any]:
    return {
        "id": "resp",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": "gpt-4.1-mini",
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "output": [
            {
                "type": "message",
                "id": "msg",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
    }


VIEW = PlannerView("restart", [{"project": "payments-ops"}], [{"tool": "search_knowledge"}], 3)


async def test_openai_planner_uses_strict_schema_and_untrusted_observations() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["text"]["format"]["strict"] is True and payload["store"] is False
        assert "UNTRUSTED" in payload["instructions"]
        assert "untrusted_observations" in json.loads(payload["input"])
        decision = {"decision": {"tool": "search_knowledge", "query": "restart"}}
        return httpx.Response(200, json=response(json.dumps(decision)))

    assert await planner(handler).decide(VIEW) == {"tool": "search_knowledge", "query": "restart"}


@pytest.mark.parametrize("failure", ["timeout", "http_500", "malformed", "forbidden_tool"])
async def test_openai_planner_failures_are_controlled(failure: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            import asyncio

            await asyncio.sleep(2)
        if failure == "http_500":
            return httpx.Response(500, json={"error": {"message": "x"}})
        body = {"decision": {"tool": "create_gitlab_issue"}} if failure == "forbidden_tool" else "{"
        return httpx.Response(
            200, json=response(json.dumps(body) if failure != "malformed" else "{")
        )

    with pytest.raises(ProviderError):
        await planner(handler).decide(VIEW)
