"""Validated environment-only configuration; secrets are never printed."""

import json
import os
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from opspilot.domain import DIMENSIONS


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_url: SecretStr
    tenant_tokens: dict[str, UUID] = Field(min_length=1, max_length=100, repr=False)
    provider: Literal["fake", "openai"] = "fake"
    openai_api_key: SecretStr | None = None
    embedding_model: str = Field(default="text-embedding-3-small", min_length=1, max_length=100)
    answer_model: str = Field(default="gpt-4.1-mini", min_length=1, max_length=100)
    provider_timeout_seconds: float = Field(default=15, ge=0.1, le=30)
    request_timeout_seconds: float = Field(default=60, ge=1, le=120)
    database_timeout_seconds: float = Field(default=5, ge=0.1, le=10)

    @field_validator("database_url")
    @classmethod
    def database_driver(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
        except ArgumentError:
            raise ValueError("expected PostgreSQL asyncpg URL") from None
        if url.drivername != "postgresql+asyncpg" or not url.database or not url.username:
            raise ValueError("expected PostgreSQL asyncpg URL")
        return value

    @field_validator("tenant_tokens")
    @classmethod
    def bounded_tokens(cls, value: dict[str, UUID]) -> dict[str, UUID]:
        if any(not 32 <= len(token) <= 128 or not token.isascii() for token in value):
            raise ValueError("tokens must contain 32 to 128 ASCII characters")
        return value

    @model_validator(mode="after")
    def provider_credentials(self) -> Self:
        if self.provider == "openai" and (
            self.openai_api_key is None or not self.openai_api_key.get_secret_value().strip()
        ):
            raise ValueError("OpenAI provider requires credentials")
        return self

    @property
    def embedding_space(self) -> str:
        return f"openai:{self.embedding_model}:{DIMENSIONS}"

    @classmethod
    def from_env(cls) -> Self:
        # Do not use .env loading: Compose loads env separately, not the application.
        tokens = json.loads(os.environ.get("TENANT_TOKENS", "{}"))
        return cls.model_validate(
            {
                "database_url": SecretStr(os.environ["DATABASE_URL"]),
                "tenant_tokens": tokens,
                "provider": os.environ.get("PROVIDER", "fake"),
                "openai_api_key": SecretStr(os.environ.get("OPENAI_API_KEY", "")),
                "embedding_model": os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"),
                "answer_model": os.environ.get("ANSWER_MODEL", "gpt-4.1-mini"),
                "provider_timeout_seconds": os.environ.get("PROVIDER_TIMEOUT_SECONDS", "15"),
                "request_timeout_seconds": os.environ.get("REQUEST_TIMEOUT_SECONDS", "60"),
                "database_timeout_seconds": os.environ.get("DATABASE_TIMEOUT_SECONDS", "5"),
            }
        )
