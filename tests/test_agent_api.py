"""HTTP contract of the agent API against real PostgreSQL and the fake GitLab server."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from opspilot.api.app import create_app
from opspilot.application import RagService
from opspilot.config import Settings
from opspilot.providers.fake import FakeProvider
from tests.agent_support import GITLAB_TOKEN, Env, service

pytestmark = pytest.mark.integration
ALICE, BOB, MALLORY, READER = "a" * 40, "b" * 40, "m" * 40, "r" * 40


@asynccontextmanager
async def client(env: Env) -> AsyncIterator[httpx.AsyncClient]:
    tokens = {
        ALICE: {"tenant": str(env.a), "subject": "alice", "roles": ["agent"]},
        BOB: {"tenant": str(env.a), "subject": "bob", "roles": ["approver"]},
        MALLORY: {"tenant": str(env.b), "subject": "mallory", "roles": ["agent", "approver"]},
        READER: {"tenant": str(env.a), "subject": "reader", "roles": []},
    }
    async with service(env) as agent:
        fake = FakeProvider()
        settings = Settings.model_validate(agent.settings.model_dump() | {"tenant_tokens": tokens})
        rag = RagService(agent.store.repository, fake, fake)
        app = create_app(settings, rag, agent)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_agent_http_flow_and_error_contract(
    agent_env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="opspilot"):
        async with client(agent_env) as http:
            assert (await http.post("/v1/agent/runs", json={"request": "x"})).status_code == 401
            forbidden = await http.post(
                "/v1/agent/runs", headers=auth(READER), json={"request": "open an issue"}
            )
            assert forbidden.status_code == 403
            assert forbidden.json()["error"] == "agent_role_required"
            smuggled = await http.post(
                "/v1/agent/runs",
                headers=auth(ALICE),
                json={"request": "x", "tenant_id": str(agent_env.b), "approved": True},
            )
            assert smuggled.status_code == 422
            created = await http.post(
                "/v1/agent/runs",
                headers=auth(ALICE),
                json={"request": "Please open an issue to restart payments-api"},
            )
            assert created.status_code == 201
            run: dict[str, Any] = created.json()
            assert run["status"] == "awaiting_approval" and run["execution"] is None
            assert run["request_id"] == created.headers["x-request-id"]
            run_id, good = run["run_id"], run["proposal"]["action_hash"]
            path = f"/v1/agent/runs/{run_id}"

            cases = [
                (ALICE, {"action_hash": good}, 403, "approver_role_required"),
                (BOB, {"action_hash": "0" * 64}, 409, "action_hash_mismatch"),
                (MALLORY, {"action_hash": good}, 404, "not_found"),
                (BOB, {"action_hash": "not-a-hash"}, 422, "invalid_input"),
            ]
            for token, body, status, code in cases:
                response = await http.post(f"{path}/approve", headers=auth(token), json=body)
                assert (response.status_code, response.json()["error"]) == (status, code)
            assert agent_env.server.state.creates_received == 0
            assert (await http.get(path, headers=auth(MALLORY))).status_code == 404

            approved = await http.post(
                f"{path}/approve", headers=auth(BOB), json={"action_hash": good}
            )
            assert approved.status_code == 200
            body = approved.json()
            assert body["status"] == "succeeded"
            assert (
                body["approval"]["decided_by"] == "bob" and body["approval"]["action_hash"] == good
            )
            assert body["execution"]["issue_url"] == agent_env.issues()[0]["web_url"]
            again = await http.post(
                f"{path}/approve", headers=auth(BOB), json={"action_hash": good}
            )
            assert (again.status_code, again.json()["error"]) == (409, "invalid_state")
            fetched = (await http.get(path, headers=auth(ALICE))).json()
            assert fetched["status"] == "succeeded" and len(agent_env.issues()) == 1
    for secret in (ALICE, BOB, GITLAB_TOKEN, "Authorization", "restart payments-api"):
        assert secret not in caplog.text
    assert '"operation":"agent.run"' in caplog.text and f'"run_id":"{run_id}"' in caplog.text
    assert '"operation":"gitlab.request"' in caplog.text
