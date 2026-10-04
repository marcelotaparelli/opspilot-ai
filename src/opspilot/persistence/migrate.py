"""One versioned transactional migration, run separately with DDL credentials."""

import asyncio
import os
from importlib.resources import files

from pydantic import BaseModel, SecretStr, field_validator
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import create_async_engine


class MigrationSettings(BaseModel):
    database_url: SecretStr

    @field_validator("database_url")
    @classmethod
    def async_postgres(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
        except ArgumentError:
            raise ValueError("expected async PostgreSQL migration URL") from None
        if url.drivername != "postgresql+asyncpg" or not url.database or not url.username:
            raise ValueError("expected async PostgreSQL migration URL")
        return value


async def migrate() -> None:
    settings = MigrationSettings(database_url=SecretStr(os.environ["MIGRATION_DATABASE_URL"]))
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        hide_parameters=True,
        connect_args={"timeout": 10, "command_timeout": 30},
    )
    try:
        async with asyncio.timeout(30):
            async with engine.begin() as connection:
                await connection.execute(text("SELECT pg_advisory_xact_lock(74183601)"))
                await connection.execute(
                    text(
                        "CREATE TABLE IF NOT EXISTS schema_version ("
                        "singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton), "
                        "version integer NOT NULL)"
                    )
                )
                result = await connection.execute(text("SELECT version FROM schema_version"))
                version = result.scalar()
                if version == 1:
                    return
                if version is not None:
                    raise RuntimeError("unsupported schema version")
                sql = files("opspilot.persistence").joinpath("schema.sql").read_text()
                for statement in sql.split(";"):
                    if statement.strip():
                        await connection.execute(text(statement))
                await connection.execute(
                    text("INSERT INTO schema_version (singleton, version) VALUES (true, 1)")
                )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(migrate())
    except Exception:
        # Connection exceptions often contain DSNs; do not emit their repr/traceback.
        raise SystemExit("Migration failed; check database configuration and privileges.") from None
