"""Real process death: a child process is SIGKILLed; a fresh process must recover."""

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from uuid import UUID

import pytest

from tests.agent_support import Env, service

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]

CHILD = textwrap.dedent(
    """
    import asyncio, sys
    from uuid import UUID
    from tests.agent_support import Env, service

    async def main() -> None:
        a, b, url, phase = UUID(sys.argv[1]), UUID(sys.argv[2]), sys.argv[3], sys.argv[4]
        env = Env(a, b, None, url)  # type: ignore[arg-type]
        async with service(env, execution_lease_seconds=2, gitlab_timeout_seconds=0.9) as svc:
            proposed = await svc.start(env.alice, "Please open an issue to restart payments-api")
            print(proposed.run.id, flush=True)
            if phase == "after_proposal":
                return
            await svc.decide(env.bob, proposed.run.id, proposed.proposal.action_hash, True)
            print("UNREACHABLE", flush=True)

    asyncio.run(main())
    """
)


def child(env: Env, phase: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", CHILD, str(env.a), str(env.b), env.url, phase],
        cwd=ROOT,
        env=os.environ | {"PYTHONPATH": str(ROOT)},
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )


async def test_process_killed_after_proposal_is_resumed_by_another_process(agent_env: Env) -> None:
    process = child(agent_env, "after_proposal")
    assert process.stdout is not None
    run_id = UUID((await asyncio.to_thread(process.stdout.readline)).strip())
    assert await asyncio.to_thread(process.wait, 30) == 0
    async with service(agent_env) as process_b:
        reopened = await process_b.get(agent_env.bob, run_id)
        assert reopened.run.status == "awaiting_approval" and reopened.proposal is not None
        done = await process_b.decide(agent_env.bob, run_id, reopened.proposal.action_hash, True)
    assert done.run.status == "succeeded" and len(agent_env.issues()) == 1


async def test_process_killed_mid_request_after_gitlab_committed(agent_env: Env) -> None:
    # GitLab creates the issue, then holds the response; the client process dies meanwhile.
    agent_env.fault("hold_after_create:20")
    process = child(agent_env, "kill_during_create")
    assert process.stdout is not None
    run_id = UUID((await asyncio.to_thread(process.stdout.readline)).strip())

    def gitlab_received_request() -> None:
        deadline = time.monotonic() + 20
        while agent_env.server.state.creates_received == 0 and time.monotonic() < deadline:
            time.sleep(0.05)

    await asyncio.to_thread(gitlab_received_request)
    assert len(agent_env.issues()) == 1, "GitLab must have committed the issue"
    process.send_signal(signal.SIGKILL)
    assert await asyncio.to_thread(process.wait, 10) == -signal.SIGKILL
    async with service(
        agent_env, execution_lease_seconds=2, gitlab_timeout_seconds=0.9
    ) as process_b:
        stuck = await process_b.get(agent_env.alice, run_id)
        assert stuck.execution is not None and stuck.execution.status == "executing"
        await asyncio.sleep(2.2)  # the dead owner's lease expires
        recovered = await process_b.resume(agent_env.alice, run_id)
        events = [event["type"] for event in await process_b.store.events(agent_env.a, run_id)]
    assert recovered.run.status == "succeeded"
    assert recovered.execution is not None and recovered.execution.issue_iid == 1
    assert len(agent_env.issues()) == 1 and agent_env.server.state.creates_received == 1
    assert "execution.reconciled" in events
