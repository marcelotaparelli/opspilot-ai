from dataclasses import replace
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from opspilot.api.app import create_app
from opspilot.application import RagService
from opspilot.config import Settings
from opspilot.domain import DependencyError, Metadata
from opspilot.persistence.postgres import PostgresRepository
from opspilot.retrieval import RetrievalMode
from tests.helpers import MALICIOUS, TOKEN_A, TOKEN_B, RecordingProvider

pytestmark = pytest.mark.integration


async def test_real_hybrid_tenant_isolation_and_injection(
    postgres: tuple[PostgresRepository, UUID, UUID], settings: Settings
) -> None:
    repository, a, b = postgres
    provider = RecordingProvider()
    service = RagService(repository, provider, provider)
    own, own_ids = await service.ingest(a, "restart runbook " + MALICIOUS, Metadata("A"))
    other, other_ids = await service.ingest(b, "restart runbook PRIVATE_B_SECRET", Metadata("B"))
    modes: tuple[RetrievalMode, ...] = ("lexical", "vector", "hybrid")
    for mode in modes:
        hits = await service.retriever.search(a, "restart runbook", 20, mode)
        assert hits
        assert {hit.chunk.tenant_id for hit in hits} == {a}
        assert all(hit.chunk.id not in other_ids for hit in hits)
    result = await service.query(a, "restart runbook", 20)
    assert {hit.chunk.document_id for hit in result.retrieved} == {own.id}
    assert {chunk.id for chunk in result.citations}.issubset(set(own_ids))
    assert "PRIVATE_B_SECRET" not in result.answer
    assert MALICIOUS in provider.contexts[0][0].text
    assert all(chunk.tenant_id == a for chunk in provider.contexts[0])
    # API authentication must fix the scope even with an adversarial tenant header.
    config = settings.model_copy(update={"tenant_tokens": {TOKEN_A: a, TOKEN_B: b}})
    app = create_app(config, service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/query",
            headers={"Authorization": f"Bearer {TOKEN_A}", "X-Tenant-ID": str(b)},
            json={"question": "restart runbook"},
        )
        assert response.status_code == 200
        assert str(other.id) not in response.text and "PRIVATE_B_SECRET" not in response.text


async def test_rls_without_application_filters_and_pool_reset(
    postgres: tuple[PostgresRepository, UUID, UUID],
) -> None:
    repository, a, b = postgres
    provider = RecordingProvider()
    service = RagService(repository, provider, provider)
    await service.ingest(a, "shared keyword", Metadata("A"))
    await service.ingest(b, "shared keyword", Metadata("B"))
    for tenant in (a, b, a, b):
        async with repository.transaction(tenant) as connection:
            result = await connection.execute(text("SELECT tenant_id FROM chunks"))
            assert set(result.scalars().all()) == {tenant}
    async with repository.engine.connect() as connection:
        result = await connection.execute(text("SELECT count(*) FROM chunks"))
        assert result.scalar_one() == 0
        with pytest.raises(SQLAlchemyError):
            await connection.execute(text("ALTER TABLE chunks DISABLE ROW LEVEL SECURITY"))


async def test_atomic_save_rollback_and_cross_tenant_write_denied(
    postgres: tuple[PostgresRepository, UUID, UUID],
) -> None:
    from uuid import uuid4

    from opspilot.chunking import split_document
    from opspilot.domain import Document

    repository, a, b = postgres
    provider = RecordingProvider()
    document = Document(uuid4(), a, "rollback sentinel", Metadata("rollback"))
    chunks = split_document(document)
    vectors = await provider.embed([chunk.text for chunk in chunks])
    # Document insert succeeds, malicious chunk insert fails RLS; document must roll back.
    with pytest.raises(DependencyError):
        await repository.save(document, [replace(chunks[0], tenant_id=b)], vectors, provider.space)
    async with repository.transaction(a) as connection:
        result = await connection.execute(
            text("SELECT count(*) FROM documents WHERE id=:id"), {"id": document.id}
        )
        assert result.scalar_one() == 0


async def test_lexical_index_and_repeatable_ranking(
    postgres: tuple[PostgresRepository, UUID, UUID],
) -> None:
    repository, a, _ = postgres
    provider = RecordingProvider()
    service = RagService(repository, provider, provider)
    await service.ingest(a, "restart service " * 150, Metadata("runbook"))
    first = await service.retriever.search(a, "restart service", 5)
    second = await service.retriever.search(a, "restart service", 5)
    assert first == second and len(first) >= 2
    async with repository.transaction(a) as connection:
        vector = await connection.execute(text("SELECT search_vector::text FROM chunks LIMIT 1"))
        assert "restart" in vector.scalar_one()
        index = await connection.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname='chunks_lexical'")
        )
        assert "USING gin" in index.scalar_one()


async def test_real_embedding_space_filter(
    postgres: tuple[PostgresRepository, UUID, UUID],
) -> None:
    repository, a, _ = postgres
    provider = RecordingProvider()
    service = RagService(repository, provider, provider)
    await service.ingest(a, "runbook", Metadata("A"))
    vector = (await provider.embed(["runbook"]))[0]
    assert await repository.vector(a, vector, provider.space, 5)
    assert not await repository.vector(a, vector, "different-model", 5)


async def test_real_http_ingestion_and_query(
    postgres: tuple[PostgresRepository, UUID, UUID], settings: Settings
) -> None:
    repository, a, b = postgres
    provider = RecordingProvider()
    config = settings.model_copy(update={"tenant_tokens": {TOKEN_A: a, TOKEN_B: b}})
    app = create_app(config, RagService(repository, provider, provider))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/documents",
            headers={"Authorization": f"Bearer {TOKEN_A}"},
            json={
                "content": "  restart service\r\ncheck health  ",
                "metadata": {"title": " Runbook ", "tags": ["OPS", "ops"]},
            },
        )
        assert created.status_code == 201
        answer = await client.post(
            "/v1/query",
            headers={"Authorization": f"Bearer {TOKEN_A}"},
            json={"question": "restart service"},
        )
        assert answer.status_code == 200
        assert answer.json()["citations"][0]["document_id"] == created.json()["document_id"]
        assert answer.json()["citations"][0]["quote"] == "restart service\ncheck health"
        assert answer.json()["request_id"] == answer.headers["x-request-id"]
        async with repository.transaction(a) as connection:
            tags = await connection.execute(
                text("SELECT tags FROM documents WHERE id=:id"),
                {"id": UUID(created.json()["document_id"])},
            )
            assert tags.scalar_one() == ["ops"]
