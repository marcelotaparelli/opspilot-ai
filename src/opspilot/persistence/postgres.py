"""SQLAlchemy async transactions with explicit filters and PostgreSQL RLS."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from opspilot.config import Settings
from opspilot.domain import Chunk, DependencyError, Document, Hit, validate_vectors


class PostgresRepository:
    def __init__(self, settings: Settings) -> None:
        self.timeout = settings.database_timeout_seconds
        self.engine = create_async_engine(
            settings.database_url.get_secret_value(),
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            pool_timeout=self.timeout,
            hide_parameters=True,
            connect_args={
                "timeout": self.timeout,
                "command_timeout": self.timeout,
                "server_settings": {"statement_timeout": str(int(self.timeout * 1000))},
            },
        )

    @asynccontextmanager
    async def transaction(self, tenant: UUID) -> AsyncIterator[AsyncConnection]:
        try:
            async with asyncio.timeout(self.timeout):
                async with self.engine.begin() as connection:
                    await connection.execute(
                        text("SELECT set_config('app.tenant_id', :tenant, true)"),
                        {"tenant": str(tenant)},
                    )
                    yield connection
        except (SQLAlchemyError, TimeoutError, OSError):
            raise DependencyError from None

    async def save(
        self, document: Document, chunks: list[Chunk], vectors: list[list[float]], space: str
    ) -> None:
        validate_vectors(vectors, len(chunks))
        async with self.transaction(document.tenant_id) as connection:
            await connection.execute(
                text(
                    "INSERT INTO documents (id, tenant_id, title, source, tags, content, "
                    "embedding_space) VALUES (:id, :tenant, :title, :source, "
                    "CAST(:tags AS jsonb), :content, :space)"
                ),
                {
                    "id": document.id,
                    "tenant": document.tenant_id,
                    "title": document.metadata.title,
                    "source": document.metadata.source,
                    "tags": json.dumps(document.metadata.tags),
                    "content": document.content,
                    "space": space,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO chunks (id, tenant_id, document_id, ordinal, content, "
                    "start_offset, end_offset, embedding, embedding_space) VALUES "
                    "(:id, :tenant, :document, :ordinal, :content, :start, :end, "
                    "CAST(:vector AS vector), :space)"
                ),
                [
                    {
                        "id": chunk.id,
                        "tenant": chunk.tenant_id,
                        "document": chunk.document_id,
                        "ordinal": chunk.ordinal,
                        "content": chunk.text,
                        "start": chunk.start,
                        "end": chunk.end,
                        "vector": json.dumps(vector, allow_nan=False),
                        "space": space,
                    }
                    for chunk, vector in zip(chunks, vectors, strict=True)
                ],
            )

    @staticmethod
    def hits(rows: list[dict[str, Any]]) -> list[Hit]:
        # Untyped DB values remain confined to this adapter.
        return [
            Hit(
                Chunk(
                    id=row["id"],
                    document_id=row["document_id"],
                    tenant_id=row["tenant_id"],
                    ordinal=row["ordinal"],
                    text=row["content"],
                    start=row["start_offset"],
                    end=row["end_offset"],
                    title=row["title"],
                    source=row["source"],
                ),
                float(row["score"]),
            )
            for row in rows
        ]

    async def vector(
        self, tenant: UUID, vector: list[float], space: str, limit: int
    ) -> list[Hit]:
        validate_vectors([vector], 1)
        async with self.transaction(tenant) as connection:
            result = await connection.execute(
                text(
                    "SELECT c.*, d.title, d.source, "
                    "1 - (c.embedding <=> CAST(:vector AS vector)) AS score "
                    "FROM chunks c JOIN documents d "
                    "ON d.id=c.document_id AND d.tenant_id=c.tenant_id "
                    "WHERE c.tenant_id=:tenant AND d.tenant_id=:tenant "
                    "AND c.embedding_space=:space "
                    "ORDER BY c.embedding <=> CAST(:vector AS vector), c.id LIMIT :limit"
                ),
                {
                    "vector": json.dumps(vector, allow_nan=False),
                    "tenant": tenant,
                    "space": space,
                    "limit": limit,
                },
            )
            return self.hits([dict(row) for row in result.mappings().all()])

    async def lexical(self, tenant: UUID, question: str, limit: int) -> list[Hit]:
        async with self.transaction(tenant) as connection:
            result = await connection.execute(
                text(
                    "SELECT c.*, d.title, d.source, "
                    "ts_rank_cd(c.search_vector, q.query) AS score "
                    "FROM chunks c JOIN documents d "
                    "ON d.id=c.document_id AND d.tenant_id=c.tenant_id "
                    "CROSS JOIN websearch_to_tsquery('simple', :question) AS q(query) "
                    "WHERE c.tenant_id=:tenant AND d.tenant_id=:tenant "
                    "AND c.search_vector @@ q.query "
                    "ORDER BY score DESC, c.id LIMIT :limit"
                ),
                {"tenant": tenant, "question": question, "limit": limit},
            )
            return self.hits([dict(row) for row in result.mappings().all()])

    async def ready(self) -> None:
        try:
            async with asyncio.timeout(self.timeout):
                async with self.engine.connect() as connection:
                    result = await connection.execute(
                        text(
                            "SELECT NOT r.rolsuper AND NOT r.rolbypassrls "
                            "AND r.rolname = 'opspilot_app' "
                            "AND (SELECT version FROM schema_version WHERE singleton) = 1 "
                            "AND EXISTS (SELECT 1 FROM pg_extension WHERE extname='vector') "
                            "AND (SELECT count(*) FROM pg_class WHERE "
                            "oid IN ('documents'::regclass, 'chunks'::regclass) "
                            "AND relrowsecurity AND relforcerowsecurity) = 2 "
                            "FROM pg_roles r WHERE r.rolname = current_user"
                        )
                    )
                    if result.scalar_one() is not True:
                        raise DependencyError
        except (SQLAlchemyError, TimeoutError, OSError):
            raise DependencyError from None

    async def close(self) -> None:
        await self.engine.dispose()
