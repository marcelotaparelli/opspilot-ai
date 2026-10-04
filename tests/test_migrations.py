"""Release migration paths on throwaway databases: empty → latest, v1 → latest, fail-fast."""

import asyncio
import os
from collections.abc import AsyncIterator
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from opspilot.config import Settings
from opspilot.persistence.migrate import LATEST, migrate
from opspilot.persistence.postgres import RLS_TABLES, PostgresRepository

pytestmark = pytest.mark.integration


def url_for(base: str, database: str) -> str:
    return make_url(base).set(database=database).render_as_string(hide_password=False)


@pytest_asyncio.fixture
async def database(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    """A fresh database prepared like scripts/init-db.sh (role already exists cluster-wide)."""
    admin = os.environ["TEST_ADMIN_DATABASE_URL"]
    name = f"migration_{uuid4().hex[:12]}"
    server = create_async_engine(admin, isolation_level="AUTOCOMMIT")
    try:
        async with server.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
            await connection.execute(text(f'GRANT CONNECT ON DATABASE "{name}" TO opspilot_app'))
        scoped = create_async_engine(url_for(admin, name))
        async with scoped.begin() as connection:
            await connection.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
            await connection.execute(text("GRANT USAGE ON SCHEMA public TO opspilot_app"))
        await scoped.dispose()
        monkeypatch.setenv("MIGRATION_DATABASE_URL", url_for(admin, name))
        yield name
    finally:
        async with server.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await server.dispose()


async def scalar(name: str, sql: str) -> object:
    engine = create_async_engine(url_for(os.environ["TEST_ADMIN_DATABASE_URL"], name))
    try:
        async with engine.connect() as connection:
            return (await connection.execute(text(sql))).scalar()
    finally:
        await engine.dispose()


async def runtime_ready(name: str) -> None:
    settings = Settings.model_validate(
        {
            "database_url": SecretStr(url_for(os.environ["TEST_DATABASE_URL"], name)),
            "tenant_tokens": {"r" * 40: str(uuid4())},
        }
    )
    repository = PostgresRepository(settings)
    try:
        await repository.ready()  # runtime role, schema version, vector, forced RLS everywhere
    finally:
        await repository.close()


async def test_empty_database_to_latest_is_idempotent_and_ready(database: str) -> None:
    await migrate()
    assert await scalar(database, "SELECT version FROM schema_version") == LATEST == 2
    forced = await scalar(
        database,
        "SELECT count(*) FROM pg_class WHERE relname = ANY(ARRAY["
        + ",".join(f"'{t}'" for t in RLS_TABLES)
        + "]) AND relforcerowsecurity",
    )
    assert forced == len(RLS_TABLES)
    await migrate()  # re-run is a no-op
    assert await scalar(database, "SELECT count(*) FROM schema_version") == 1
    await runtime_ready(database)


async def test_v1_database_upgrades_to_latest_and_keeps_data(database: str) -> None:
    engine = create_async_engine(os.environ["MIGRATION_DATABASE_URL"])
    document = uuid4()
    try:
        async with engine.begin() as connection:
            # Exactly what a v1 deployment looked like (Phase 1 release).
            await connection.execute(
                text(
                    "CREATE TABLE schema_version (singleton boolean PRIMARY KEY DEFAULT true "
                    "CHECK (singleton), version integer NOT NULL)"
                )
            )
            v1 = files("opspilot.persistence").joinpath("schema.sql").read_text()
            for statement in v1.split(";"):
                if statement.strip():
                    await connection.execute(text(statement))
            await connection.execute(text("INSERT INTO schema_version VALUES (true, 1)"))
            await connection.execute(
                text(
                    "INSERT INTO documents (id, tenant_id, title, content, embedding_space) "
                    "VALUES (:id, :tenant, 'kept', 'v1 content', 'fake')"
                ),
                {"id": document, "tenant": uuid4()},
            )
    finally:
        await engine.dispose()
    assert await scalar(database, "SELECT to_regclass('agent_runs') IS NULL") is True
    await migrate()
    assert await scalar(database, "SELECT version FROM schema_version") == 2
    assert await scalar(database, "SELECT to_regclass('agent_runs') IS NOT NULL") is True
    assert await scalar(database, f"SELECT count(*) FROM documents WHERE id = '{document}'") == 1
    await runtime_ready(database)


async def test_unknown_future_version_fails_fast(database: str) -> None:
    await migrate()
    engine = create_async_engine(os.environ["MIGRATION_DATABASE_URL"])
    try:
        async with engine.begin() as connection:
            await connection.execute(text("UPDATE schema_version SET version = 99"))
    finally:
        await engine.dispose()
    with pytest.raises(RuntimeError, match="unsupported schema version"):
        await migrate()


async def test_concurrent_migrators_serialise_on_the_advisory_lock(database: str) -> None:
    await asyncio.gather(migrate(), migrate(), migrate())
    assert await scalar(database, "SELECT version FROM schema_version") == 2
    assert await scalar(database, "SELECT count(*) FROM schema_version") == 1


async def test_failed_ddl_rolls_back_all_migrations_and_can_retry(
    database: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for filename in ("schema.sql", "schema_v2.sql"):
        sql = files("opspilot.persistence").joinpath(filename).read_text()
        if filename == "schema_v2.sql":
            sql += "\nSELECT 1 / 0;"
        (tmp_path / filename).write_text(sql)
    with monkeypatch.context() as scoped:
        scoped.setattr("opspilot.persistence.migrate.files", lambda _: tmp_path)
        with pytest.raises(DBAPIError):
            await migrate()
    for table in ("schema_version", "documents", "agent_runs"):
        assert await scalar(database, f"SELECT to_regclass('{table}') IS NULL") is True
    await migrate()
    assert await scalar(database, "SELECT version FROM schema_version") == LATEST
    await runtime_ready(database)
