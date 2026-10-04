import pytest
from pydantic import SecretStr, ValidationError

from opspilot.config import Settings
from tests.helpers import TENANT_A, TOKEN_A


@pytest.mark.parametrize(
    "url",
    ["sqlite+aiosqlite:///app.db", "postgresql://user:secret@localhost/app", "not-a-url"],
)
def test_async_postgres_required(url: str) -> None:
    with pytest.raises((ValidationError, ValueError)):
        Settings(database_url=SecretStr(url), tenant_tokens={TOKEN_A: TENANT_A})


def test_openai_requires_secret(settings: Settings) -> None:
    data = settings.model_dump()
    data["provider"] = "openai"
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


def test_configuration_repr_redacts_credentials(settings: Settings) -> None:
    rendered = repr(settings)
    assert TOKEN_A not in rendered
    assert "postgresql+asyncpg" not in rendered


def test_environment_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://opspilot_app:secret@localhost/test")
    monkeypatch.setenv("TENANT_TOKENS", '{"short":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}')
    with pytest.raises(ValidationError):
        Settings.from_env()
