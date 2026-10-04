"""Validated environment-only configuration; secrets are never printed."""

import hashlib
import json
import os
from typing import Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from opspilot.agent.policy import AgentPolicy, Principal
from opspilot.domain import DIMENSIONS
from opspilot.pricing import PricingTable


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_url: SecretStr
    # Token -> principal. A bare tenant UUID (Phase 1 format) maps to a principal with the
    # "agent" role and a subject derived from the token hash; approvers must be explicit.
    tenant_tokens: dict[str, Principal] = Field(min_length=1, max_length=100, repr=False)
    # "production" turns known development defaults into startup errors (fail-fast).
    app_env: Literal["development", "production"] = "development"
    expose_api_docs: bool = False
    provider: Literal["fake", "openai"] = "fake"
    openai_api_key: SecretStr | None = None
    embedding_model: str = Field(default="text-embedding-3-small", min_length=1, max_length=100)
    answer_model: str = Field(default="gpt-4.1-mini", min_length=1, max_length=100)
    provider_timeout_seconds: float = Field(default=15, ge=0.1, le=30)
    request_timeout_seconds: float = Field(default=60, ge=1, le=120)
    database_timeout_seconds: float = Field(default=5, ge=0.1, le=10)
    agent_policy: AgentPolicy = Field(default_factory=AgentPolicy)
    agent_max_steps: int = Field(default=6, ge=1, le=20)
    agent_deadline_seconds: float = Field(default=45, ge=1, le=110)
    agent_llm_timeout_seconds: float = Field(default=20, ge=0.1, le=60)
    gitlab_allow_http: bool = False
    gitlab_base_url: str | None = None
    gitlab_token: SecretStr | None = Field(default=None, repr=False)
    gitlab_timeout_seconds: float = Field(default=10, ge=0.05, le=30)
    execution_lease_seconds: float = Field(default=60, ge=0.1, le=600)
    reconcile_grace_seconds: float = Field(default=30, ge=0, le=3600)
    max_execution_attempts: int = Field(default=3, ge=1, le=5)
    # OTLP/HTTP collector base URL (e.g. http://otel-collector:4318); unset = no export.
    otel_exporter_otlp_endpoint: str | None = None
    # Configured prices only; an unpriced model has unknown cost, never an invented one.
    model_pricing: PricingTable = Field(default_factory=PricingTable)

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

    @field_validator("tenant_tokens", mode="before")
    @classmethod
    def legacy_tokens(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        return {
            token: {
                "tenant": principal,
                "subject": f"token-{hashlib.sha256(str(token).encode()).hexdigest()[:12]}",
                "roles": ["agent"],
            }
            if isinstance(principal, str | UUID)
            else principal
            for token, principal in value.items()
        }

    @field_validator("tenant_tokens")
    @classmethod
    def bounded_tokens(cls, value: dict[str, Principal]) -> dict[str, Principal]:
        if any(not 32 <= len(token) <= 128 or not token.isascii() for token in value):
            raise ValueError("tokens must contain 32 to 128 ASCII characters")
        return value

    @model_validator(mode="after")
    def provider_credentials(self) -> Self:
        if self.provider == "openai" and (
            self.openai_api_key is None or not self.openai_api_key.get_secret_value().strip()
        ):
            raise ValueError("OpenAI provider requires credentials")
        if self.agent_deadline_seconds >= self.request_timeout_seconds:
            raise ValueError("agent deadline must be shorter than the request deadline")
        if self.execution_lease_seconds <= self.gitlab_timeout_seconds:
            raise ValueError("execution lease must outlast one GitLab request")
        if (self.gitlab_base_url is None) != (self.gitlab_token is None):
            raise ValueError("GitLab requires both base URL and token")
        if self.app_env == "production":
            self.production_checks()
        return self

    def production_checks(self) -> None:
        """Refuse to start production with development shortcuts or published examples."""
        if self.gitlab_allow_http:
            raise ValueError("GITLAB_ALLOW_HTTP is not allowed in production")
        if self.expose_api_docs:
            raise ValueError("API docs must not be exposed in production")
        published = (
            "replace-with",
            "random-token-at-least",
            "development-",
            "ci-token",
            "change-me",
        )
        values = [*self.tenant_tokens, self.database_url.get_secret_value()]
        if self.gitlab_token is not None:
            values.append(self.gitlab_token.get_secret_value())
        if any(marker in value for value in values for marker in published):
            raise ValueError("example credentials are not allowed in production")

    @field_validator("otel_exporter_otlp_endpoint")
    @classmethod
    def otlp_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.path not in ("", "/")
        ):
            raise ValueError("OTLP endpoint must be http(s)://host[:port] without credentials")
        return value.rstrip("/")

    @field_validator("gitlab_base_url")
    @classmethod
    def gitlab_url(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        allowed = {"https", "http"} if info.data.get("gitlab_allow_http") else {"https"}
        if (
            parts.scheme not in allowed
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path not in ("", "/")
        ):
            raise ValueError("GitLab base URL must be https://host[:port] without credentials")
        return value.rstrip("/")

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
                "app_env": os.environ.get("APP_ENV", "development"),
                "expose_api_docs": os.environ.get("EXPOSE_API_DOCS", "false") == "true",
                "provider": os.environ.get("PROVIDER", "fake"),
                "openai_api_key": SecretStr(os.environ.get("OPENAI_API_KEY", "")),
                "embedding_model": os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"),
                "answer_model": os.environ.get("ANSWER_MODEL", "gpt-4.1-mini"),
                "provider_timeout_seconds": os.environ.get("PROVIDER_TIMEOUT_SECONDS", "15"),
                "request_timeout_seconds": os.environ.get("REQUEST_TIMEOUT_SECONDS", "60"),
                "database_timeout_seconds": os.environ.get("DATABASE_TIMEOUT_SECONDS", "5"),
                "agent_policy": {"tenants": json.loads(os.environ.get("AGENT_POLICY") or "{}")},
                "agent_max_steps": os.environ.get("AGENT_MAX_STEPS", "6"),
                "agent_deadline_seconds": os.environ.get("AGENT_DEADLINE_SECONDS", "45"),
                "gitlab_allow_http": os.environ.get("GITLAB_ALLOW_HTTP", "false") == "true",
                "gitlab_base_url": os.environ.get("GITLAB_BASE_URL") or None,
                "gitlab_token": SecretStr(token)
                if (token := os.environ.get("GITLAB_TOKEN"))
                else None,
                "gitlab_timeout_seconds": os.environ.get("GITLAB_TIMEOUT_SECONDS", "10"),
                "execution_lease_seconds": os.environ.get("EXECUTION_LEASE_SECONDS", "60"),
                "reconcile_grace_seconds": os.environ.get("RECONCILE_GRACE_SECONDS", "30"),
                "otel_exporter_otlp_endpoint": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
                or None,
                "model_pricing": {"prices": json.loads(os.environ.get("MODEL_PRICING") or "[]")},
            }
        )
