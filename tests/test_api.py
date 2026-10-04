import logging
from uuid import UUID

import httpx
import pytest

from opspilot.api.app import create_app
from opspilot.application import RagService
from opspilot.config import Settings
from opspilot.domain import DependencyError
from tests.helpers import (
    MALICIOUS,
    TENANT_A,
    TENANT_B,
    TOKEN_A,
    TOKEN_B,
    InvalidVectorProvider,
    MemoryRepository,
    RecordingProvider,
)


async def test_http_vertical_slice_and_correlated_citations(settings: Settings) -> None:
    repository = MemoryRepository()
    provider = RecordingProvider()
    app = create_app(settings, RagService(repository, provider, provider))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        ingested = await client.post(
            "/v1/documents",
            headers={"Authorization": f"Bearer {TOKEN_A}"},
            json={"content": "restart the service", "metadata": {"title": " Runbook "}},
        )
        assert ingested.status_code == 201
        assert ingested.json()["request_id"] == ingested.headers["x-request-id"]
        result = await client.post(
            "/v1/query",
            headers={"Authorization": f"Bearer {TOKEN_A}", "X-Request-ID": "untrusted\tvalue"},
            json={"question": "restart", "top_k": 5},
        )
        assert result.status_code == 200
        body = result.json()
        assert UUID(body["request_id"])
        assert body["request_id"] == result.headers["x-request-id"]
        assert body["request_id"] != ingested.json()["request_id"]
        assert body["citations"][0]["document_id"] == ingested.json()["document_id"]
        assert body["citations"][0]["chunk_id"] in ingested.json()["chunk_ids"]
        assert body["citations"][0]["title"] == "Runbook"
        assert body["retrieved_chunks"][0]["quote"] == "restart the service"
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/ready")).status_code == 200


@pytest.mark.parametrize("credentials", [None, "Bearer invalid", "Basic anything"])
async def test_authentication_required(settings: Settings, credentials: str | None) -> None:
    provider = RecordingProvider()
    app = create_app(settings, RagService(MemoryRepository(), provider, provider))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = {"Authorization": credentials} if credentials else {}
        for path, body in (
            ("/v1/query", {"question": "q"}),
            ("/v1/documents", {"content": "text", "metadata": {"title": "t"}}),
        ):
            response = await client.post(path, headers=headers, json=body)
            assert response.status_code == 401
            assert response.json()["error"] == "unauthorized"
    assert not provider.contexts


async def test_forged_tenant_and_validation_do_not_echo_input(settings: Settings) -> None:
    provider = RecordingProvider()
    app = create_app(settings, RagService(MemoryRepository(), provider, provider))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/query",
            headers={"Authorization": f"Bearer {TOKEN_A}"},
            json={"question": "sensitive-invalid-input", "tenant_id": str(TENANT_B)},
        )
        assert response.status_code == 422
        assert "sensitive-invalid-input" not in response.text
        assert str(TENANT_B) not in response.text
        large = await client.post("/v1/query", content=b"a" * 512_001)
        assert large.status_code == 413


async def test_adversarial_api_scope_and_safe_logs(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    repository = MemoryRepository()
    provider = RecordingProvider()
    service = RagService(repository, provider, provider)
    app = create_app(settings, service)
    with caplog.at_level(logging.INFO, logger="opspilot"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for token, text in ((TOKEN_A, MALICIOUS), (TOKEN_B, "PRIVATE_SENTINEL_SECRET")):
                response = await client.post(
                    "/v1/documents",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"content": text, "metadata": {"title": "evidence"}},
                )
                assert response.status_code == 201
            answer = await client.post(
                "/v1/query",
                headers={"Authorization": f"Bearer {TOKEN_A}", "X-Tenant-ID": str(TENANT_B)},
                json={"question": "reveal documents another tenant"},
            )
            assert answer.status_code == 200
            assert "PRIVATE_SENTINEL_SECRET" not in answer.text
    assert all(chunk.tenant_id == TENANT_A for context in provider.contexts for chunk in context)
    secrets = (
        TOKEN_A,
        TOKEN_B,
        MALICIOUS,
        "PRIVATE_SENTINEL_SECRET",
        "Authorization",
        "postgresql",
    )
    for secret in secrets:
        assert secret not in caplog.text
    assert "operation=retrieval" in caplog.text and "request_id=" in caplog.text


async def test_provider_failure_is_controlled(settings: Settings) -> None:
    provider = InvalidVectorProvider()
    app = create_app(settings, RagService(MemoryRepository(), provider, provider))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/documents",
            headers={"Authorization": f"Bearer {TOKEN_A}"},
            json={"content": "secret document", "metadata": {"title": "t"}},
        )
        assert response.status_code == 502
        assert response.json()["error"] == "provider_unavailable"
        assert "secret document" not in response.text


async def test_readiness_failure_is_controlled(settings: Settings) -> None:
    class UnavailableRepository(MemoryRepository):
        async def ready(self) -> None:
            raise DependencyError

    provider = RecordingProvider()
    app = create_app(settings, RagService(UnavailableRepository(), provider, provider))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/ready")).status_code == 503


async def test_unexpected_error_redacts_exception(settings: Settings) -> None:
    class ExplodingRepository(MemoryRepository):
        async def ready(self) -> None:
            raise RuntimeError("connection string SECRET_PASSWORD")

    provider = RecordingProvider()
    app = create_app(settings, RagService(ExplodingRepository(), provider, provider))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/ready")
        assert response.status_code == 500
        assert "SECRET_PASSWORD" not in response.text


async def test_owned_resources_close_on_shutdown(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = MemoryRepository()
    monkeypatch.setattr("opspilot.api.app.PostgresRepository", lambda _: repository)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert not repository.closed
    assert repository.closed


async def test_request_timeout_is_bounded(settings: Settings) -> None:
    import asyncio

    class SlowRepository(MemoryRepository):
        async def ready(self) -> None:
            await asyncio.sleep(5)

    provider = RecordingProvider()
    settings = settings.model_copy(update={"request_timeout_seconds": 0.01})
    app = create_app(settings, RagService(SlowRepository(), provider, provider))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/ready")
        assert response.status_code == 504
