"""Strict, bounded HTTP contracts, with metadata normalization."""

import unicodedata
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from opspilot.chunking import normalize_content

BoundedTag = Annotated[str, Field(min_length=1, max_length=40)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)


def clean(value: str) -> str:
    result = unicodedata.normalize("NFC", value).strip()
    if any(unicodedata.category(char).startswith("C") for char in result):
        raise ValueError("control characters are not allowed")
    return result


class MetadataInput(Contract):
    title: str = Field(min_length=1, max_length=200)
    source: str | None = Field(default=None, min_length=1, max_length=500)
    tags: list[BoundedTag] = Field(default_factory=list, max_length=20)

    @field_validator("title", "source", mode="before")
    @classmethod
    def normalized_text(cls, value: object) -> object:
        return clean(value) if isinstance(value, str) else value

    @field_validator("tags", mode="before")
    @classmethod
    def normalized_tags(cls, value: object) -> object:
        if isinstance(value, list):
            # Bound before deduplication so oversized arrays cannot bypass validation.
            if len(value) > 20:
                raise ValueError("too many tags")
            if all(isinstance(tag, str) for tag in value):
                return sorted({clean(tag).casefold() for tag in value})
        return value


class DocumentInput(Contract):
    content: str = Field(min_length=1, max_length=100_000)
    metadata: MetadataInput

    @model_validator(mode="after")
    def normalized_document(self) -> Self:
        self.content = normalize_content(self.content)
        return self


class DocumentOutput(Contract):
    document_id: UUID
    chunk_ids: list[UUID]
    request_id: str


class QueryInput(Contract):
    question: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)

    @field_validator("question", mode="before")
    @classmethod
    def normalized_question(cls, value: object) -> object:
        return clean(value) if isinstance(value, str) else value


class Citation(Contract):
    chunk_id: UUID
    document_id: UUID
    ordinal: int
    title: str
    source: str | None
    start_offset: int
    end_offset: int
    quote: str


class RetrievedChunk(Citation):
    score: float


class QueryOutput(Contract):
    answer: str
    citations: list[Citation]
    retrieved_chunks: list[RetrievedChunk]
    request_id: str


class ErrorOutput(Contract):
    error: str
    request_id: str
