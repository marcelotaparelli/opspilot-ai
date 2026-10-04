import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from opspilot.config import Settings
from opspilot.persistence.postgres import PostgresRepository
from tests.helpers import TENANT_A, TENANT_B, TOKEN_A, TOKEN_B


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url=SecretStr("postgresql+asyncpg://opspilot_app:test@localhost/opspilot_test"),
        tenant_tokens={TOKEN_A: TENANT_A, TOKEN_B: TENANT_B},
    )


@pytest_asyncio.fixture
async def postgres() -> AsyncIterator[tuple[PostgresRepository, UUID, UUID]]:
    # Fail, never silently skip the required real-database check.
    url = os.environ.get("TEST_DATABASE_URL")
    admin_url = os.environ.get("TEST_ADMIN_DATABASE_URL")
    if not url or not admin_url:
        pytest.fail("Real integration tests require TEST_DATABASE_URL and TEST_ADMIN_DATABASE_URL")
    repository = PostgresRepository(
        Settings(database_url=SecretStr(url), tenant_tokens={TOKEN_A: uuid4()})
    )
    a, b = uuid4(), uuid4()
    admin = create_async_engine(admin_url, hide_parameters=True)
    try:
        await repository.ready()
        yield repository, a, b
    finally:
        try:
            async with admin.begin() as connection:
                await connection.execute(
                    text("DELETE FROM documents WHERE tenant_id IN (:a, :b)"), {"a": a, "b": b}
                )
        finally:
            await admin.dispose()
            await repository.close()
