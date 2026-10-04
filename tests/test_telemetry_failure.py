"""Telemetry is fail-open: a dead collector never fails or slows requests; export recovers."""

import json
import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.agent_support import Env

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]

CHILD = textwrap.dedent(
    """
    import asyncio, json, logging, statistics, sys, threading, time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from uuid import UUID

    import httpx

    from opspilot import observability

    port, a, b, gitlab = int(sys.argv[1]), UUID(sys.argv[2]), UUID(sys.argv[3]), sys.argv[4]
    assert observability.setup(f"http://127.0.0.1:{port}")
    for handler in logging.getLogger("opspilot").handlers:
        handler.setStream(sys.stderr)

    from opspilot.api.app import create_app
    from opspilot.application import RagService
    from opspilot.config import Settings
    from opspilot.providers.fake import FakeProvider
    from tests.agent_support import Env, service

    received = {"/v1/traces": 0, "/v1/metrics": 0}

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            received[self.path] = received.get(self.path, 0) + 1
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            return

    async def exercise(http, tokens, rounds):
        statuses, latencies = [], []
        for _ in range(rounds):
            for method, path, token, body in (
                ("POST", "/v1/query", "r", {"question": "restart payments-api"}),
                ("POST", "/v1/agent/runs", "r", {"request": "How do I restart payments-api?"}),
                ("POST", "/v1/query", None, {"question": "x"}),
            ):
                headers = {"Authorization": f"Bearer {tokens[token]}"} if token else {}
                started = time.monotonic()
                response = await http.request(method, path, headers=headers, json=body)
                latencies.append(time.monotonic() - started)
                statuses.append(response.status_code)
        return statuses, latencies

    async def main():
        env = Env(a, b, None, gitlab)
        tokens = {"r": "r" * 40, "p": "p" * 40}
        principals = {
            tokens["r"]: {"tenant": str(a), "subject": "alice", "roles": ["agent"]},
            tokens["p"]: {"tenant": str(a), "subject": "bob", "roles": ["approver"]},
        }
        result = {}
        async with service(env) as agent:
            data = agent.settings.model_dump() | {"tenant_tokens": principals}
            settings = Settings.model_validate(data)
            fake = FakeProvider()
            app = create_app(settings, RagService(agent.store.repository, fake, fake), agent)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
                # Phase 1: collector down for the whole phase.
                statuses, latencies = await exercise(http, tokens, 15)
                requester = {"Authorization": f"Bearer {tokens['r']}"}
                approver = {"Authorization": f"Bearer {tokens['p']}"}
                issue = {"request": "Please open an issue to restart payments-api"}
                run = (await http.post("/v1/agent/runs", headers=requester, json=issue)).json()
                decision = {"action_hash": run["proposal"]["action_hash"]}
                approve = f"/v1/agent/runs/{run['run_id']}/approve"
                approved = await http.post(approve, headers=approver, json=decision)
                await asyncio.sleep(1.5)  # let background exports fail
                result["down"] = {
                    "statuses": sorted(set(statuses)),
                    "p95": statistics.quantiles(latencies, n=20)[18],
                    "max": max(latencies),
                    "issue_status": approved.json()["status"],
                    "received": dict(received),
                }
                # Phase 2: the collector comes back on the same endpoint.
                server = ThreadingHTTPServer(("127.0.0.1", port), Receiver)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                statuses, _ = await exercise(http, tokens, 3)
                tracer_provider, meter_provider = observability._providers
                tracer_provider.force_flush(5000)
                meter_provider.force_flush(5000)
                result["up"] = {"statuses": sorted(set(statuses)), "received": dict(received)}
        started = time.monotonic()
        observability.shutdown()
        result["shutdown_seconds"] = time.monotonic() - started
        print(json.dumps(result), flush=True)

    asyncio.run(main())
    """
)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_dead_collector_never_breaks_product_and_export_recovers(agent_env: Env) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            CHILD,
            str(free_port()),
            str(agent_env.a),
            str(agent_env.b),
            agent_env.url,
        ],
        cwd=ROOT,
        env=os.environ | {"PYTHONPATH": str(ROOT), "OTEL_BSP_SCHEDULE_DELAY": "200"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    print("telemetry-failure result:", json.dumps(result))
    down, up = result["down"], result["up"]
    assert down["statuses"] == [200, 201, 401]  # RAG and agent work; auth stays fail-closed
    assert down["issue_status"] == "succeeded"
    assert down["received"] == {"/v1/traces": 0, "/v1/metrics": 0}
    assert down["p95"] < 0.5 and down["max"] < 2.0, down  # export never sits on the request path
    assert up["statuses"] == [200, 201, 401]
    assert up["received"]["/v1/traces"] >= 1 and up["received"]["/v1/metrics"] >= 1
    assert result["shutdown_seconds"] < 10
