import os
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from opspilot.config import Settings
from opspilot.persistence.postgres import PostgresRepository
from tests.helpers import TENANT_A, TENANT_B, TOKEN_A, TOKEN_B

if TYPE_CHECKING:
    from tests.agent_support import Env
    from tests.telemetry_support import Telemetry


@pytest.fixture(scope="session", autouse=True)
def _telemetry_sdk() -> "Telemetry":
    from tests.telemetry_support import install

    return install()


@pytest.fixture
def telemetry(_telemetry_sdk: "Telemetry") -> "Telemetry":
    _telemetry_sdk.clear()
    return _telemetry_sdk


@pytest.fixture
def settings() -> Settings:
    return Settings.model_validate(
        {
            "database_url": SecretStr(
                "postgresql+asyncpg://opspilot_app:test@localhost/opspilot_test"
            ),
            "tenant_tokens": {TOKEN_A: str(TENANT_A), TOKEN_B: str(TENANT_B)},
        }
    )


@pytest_asyncio.fixture
async def postgres() -> AsyncIterator[tuple[PostgresRepository, UUID, UUID]]:
    # Fail, never silently skip the required real-database check.
    url = os.environ.get("TEST_DATABASE_URL")
    admin_url = os.environ.get("TEST_ADMIN_DATABASE_URL")
    if not url or not admin_url:
        pytest.fail("Real integration tests require TEST_DATABASE_URL and TEST_ADMIN_DATABASE_URL")
    repository = PostgresRepository(
        Settings.model_validate(
            {"database_url": SecretStr(url), "tenant_tokens": {TOKEN_A: str(uuid4())}}
        )
    )
    a, b = uuid4(), uuid4()
    admin = create_async_engine(admin_url, hide_parameters=True)
    try:
        await repository.ready()
        yield repository, a, b
    finally:
        try:
            async with admin.begin() as connection:
                # Children first: agent tables reference runs without ON DELETE CASCADE.
                for table in (
                    "agent_events",
                    "agent_executions",
                    "agent_approvals",
                    "agent_proposals",
                    "agent_runs",
                    "documents",
                ):
                    await connection.execute(
                        text(f"DELETE FROM {table} WHERE tenant_id IN (:a, :b)"), {"a": a, "b": b}
                    )
        finally:
            await admin.dispose()
            await repository.close()


@pytest_asyncio.fixture
async def agent_env(
    postgres: tuple[PostgresRepository, UUID, UUID],
) -> AsyncIterator["Env"]:
    from opspilot.application import RagService
    from opspilot.domain import Metadata
    from opspilot.providers.fake import FakeProvider
    from tests.agent_support import (
        INJECTION_APPROVAL,
        INJECTION_PROJECT,
        Env,
        start_fake_gitlab,
    )

    repository, a, b = postgres
    fake = FakeProvider()
    rag = RagService(repository, fake, fake)
    await rag.ingest(
        a, "Payments restart runbook: drain traffic, restart payments-api.", Metadata("Restart")
    )
    await rag.ingest(a, f"Restart payments-api checklist. {INJECTION_APPROVAL}", Metadata("Inj1"))
    await rag.ingest(a, f"Payments-api escalation. {INJECTION_PROJECT}", Metadata("Inj2"))
    await rag.ingest(b, "Tenant B payments restart PRIVATE_B_RUNBOOK", Metadata("B"))
    server, url = start_fake_gitlab()
    try:
        yield Env(a, b, server, url)
    finally:
        server.shutdown()
        server.server_close()
