"""Framework-independent values and errors used by application ports."""

import math
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

DIMENSIONS = 256
EMBEDDING_SPACE_FAKE = "fake:sha256-bow-v1:256"
ABSTENTION = "Insufficient evidence in the available documents."


class AppError(Exception):
    """Only a fixed error code crosses the HTTP/log boundary."""

    code = "application_error"
    status = 500


class ProviderError(AppError):
    code = "provider_unavailable"
    status = 502


class DependencyError(AppError):
    code = "persistence_unavailable"
    status = 503


class IsolationError(AppError):
    code = "isolation_violation"
    status = 500


class InvalidInput(AppError):
    code = "invalid_input"
    status = 422


@dataclass(frozen=True)
class Metadata:
    title: str
    source: str | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Document:
    id: UUID
    tenant_id: UUID
    content: str
    metadata: Metadata


@dataclass(frozen=True)
class Chunk:
    id: UUID
    document_id: UUID
    tenant_id: UUID
    ordinal: int
    text: str
    start: int
    end: int
    title: str
    source: str | None


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float


@dataclass(frozen=True)
class GeneratedAnswer:
    answer: str
    cited_chunk_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class QueryResult:
    answer: str
    citations: tuple[Chunk, ...]
    retrieved: tuple[Hit, ...]


def validate_vectors(vectors: list[list[float]], count: int) -> None:
    if len(vectors) != count:
        raise ProviderError
    for vector in vectors:
        if (
            len(vector) != DIMENSIONS
            or not all(math.isfinite(v) for v in vector)
            or not any(v != 0 for v in vector)
        ):
            raise ProviderError


class Embedder(Protocol):
    @property
    def space(self) -> str: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class Answerer(Protocol):
    async def answer(self, question: str, chunks: tuple[Chunk, ...]) -> GeneratedAnswer: ...


class Repository(Protocol):
    async def save(
        self, document: Document, chunks: list[Chunk], vectors: list[list[float]], space: str
    ) -> None: ...

    async def vector(
        self, tenant: UUID, vector: list[float], space: str, limit: int
    ) -> list[Hit]: ...

    async def lexical(self, tenant: UUID, question: str, limit: int) -> list[Hit]: ...

    async def ready(self) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class Usage:
    """Normalised provider token usage. None means unknown, never zero."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    @property
    def known(self) -> bool:
        return self.input_tokens is not None and self.output_tokens is not None
