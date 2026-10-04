"""Production-safety configuration: public surface, error envelopes, fail-fast settings."""

from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from opspilot.api.app import create_app
from opspilot.application import RagService
from opspilot.config import Settings
from tests.helpers import TENANT_A, MemoryRepository, RecordingProvider
from tests.telemetry_support import Telemetry

STRONG = "k3Jx9QpL2vR8tY6wZ1aB4cD7eF0gH5iJ"  # not an example value


def settings(**fields: Any) -> Settings:
    data: dict[str, Any] = {
        "database_url": SecretStr(
            "postgresql+asyncpg://opspilot_app:Zr4u8Wq2@db.internal/opspilot"
        ),
        "tenant_tokens": {STRONG: str(TENANT_A)},
    }
    return Settings.model_validate(data | fields)


async def client(config: Settings) -> httpx.AsyncClient:
    provider = RecordingProvider()
    app = create_app(config, RagService(MemoryRepository(), provider, provider))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_public_surface_is_intentional() -> None:
    async with await client(settings()) as http:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await http.get(path)).status_code == 404, path
        assert (await http.get("/health")).status_code == 200
    async with await client(settings(expose_api_docs=True)) as http:
        assert (await http.get("/openapi.json")).status_code == 200  # explicit opt-in only


@pytest.mark.parametrize(
    ("method", "path", "status", "code"),
    [
        ("GET", "/nope", 404, "not_found"),
        ("DELETE", "/health", 405, "method_not_allowed"),
        ("POST", "/v1/query", 401, "unauthorized"),
    ],
)
async def test_every_error_uses_one_envelope(
    method: str, path: str, status: int, code: str
) -> None:
    async with await client(settings()) as http:
        response = await http.request(method, path, json={"question": "q"})
    assert response.status_code == status
    body = response.json()
    assert body == {"error": code, "request_id": response.headers["x-request-id"]}
    assert "detail" not in body and "server" not in response.headers


async def test_framework_native_telemetry_stays_disabled(telemetry: Telemetry) -> None:
    async with await client(settings()) as http:
        await http.get("/health")
    names = {span.name for span in telemetry.spans()}
    assert names == {"http.request"}  # no fastapi.* / starlette spans outside our allowlist
    assert not any(name.startswith("http.server.") for name, _ in telemetry.metrics())


@pytest.mark.parametrize(
    "fields",
    [
        {
            "gitlab_allow_http": True,
            "gitlab_base_url": "http://gitlab.internal",
            "gitlab_token": SecretStr(STRONG),
        },
        {"expose_api_docs": True},
        {"tenant_tokens": {"replace-with-random-token-at-least-32-characters": str(TENANT_A)}},
        {
            "database_url": SecretStr(
                "postgresql+asyncpg://opspilot_app:development-app-password@db/opspilot"
            )
        },
        {
            "gitlab_base_url": "https://gitlab.example.com",
            "gitlab_token": SecretStr("change-me-" + STRONG),
        },
    ],
)
def test_production_refuses_development_shortcuts(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        settings(app_env="production", **fields)


def test_production_accepts_a_clean_configuration() -> None:
    config = settings(
        app_env="production",
        gitlab_base_url="https://gitlab.example.com",
        gitlab_token=SecretStr("glpat-" + STRONG),
    )
    assert config.app_env == "production" and not config.expose_api_docs


def test_startup_fails_fast_on_invalid_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://opspilot_app:x@db/opspilot")
    monkeypatch.setenv(
        "TENANT_TOKENS",
        '{"replace-with-random-token-at-least-32-characters": "' + str(TENANT_A) + '"}',
    )
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(RuntimeError, match="Invalid environment configuration"):
        create_app()
