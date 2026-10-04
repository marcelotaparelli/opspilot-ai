from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from opspilot.api.contracts import DocumentInput, MetadataInput, QueryInput
from opspilot.application import RagService
from opspilot.chunking import CHUNK_SIZE, MAX_CONTENT, normalize_content, split_document
from opspilot.domain import (
    ABSTENTION,
    Chunk,
    Document,
    GeneratedAnswer,
    Hit,
    InvalidInput,
    IsolationError,
    Metadata,
    ProviderError,
    validate_vectors,
)
from opspilot.providers.fake import FakeProvider
from opspilot.retrieval import rrf
from tests.helpers import (
    MALICIOUS,
    TENANT_A,
    TENANT_B,
    InvalidVectorProvider,
    MemoryRepository,
    RecordingProvider,
)


@pytest.mark.parametrize("length", [1, 1200, 1201, 10_000, MAX_CONTENT])
def test_chunk_coverage_offsets_and_ids(length: int) -> None:
    document = Document(uuid4(), TENANT_A, "a" * length, Metadata("test"))
    chunks = split_document(document)
    assert chunks == split_document(document)
    assert chunks[0].start == 0 and chunks[-1].end == length
    assert all(chunk.text == document.content[chunk.start : chunk.end] for chunk in chunks)
    assert all(0 < len(chunk.text) <= CHUNK_SIZE for chunk in chunks)
    assert all(left.end >= right.start for left, right in zip(chunks, chunks[1:], strict=False))
    assert len({chunk.id for chunk in chunks}) == len(chunks)
    assert len(chunks) <= 100


def test_normalization() -> None:
    assert normalize_content("  Cafe\u0301\r\nnext\rline  ") == "Café\nnext\nline"
    metadata = MetadataInput(title="  Cafe\u0301  ", tags=[" Z ", "a", "A"])
    assert metadata.title == "Café" and metadata.tags == ["a", "z"]


@pytest.mark.parametrize("content", [" ", "\x00test", "a" * (MAX_CONTENT + 1)])
def test_invalid_content(content: str) -> None:
    with pytest.raises(InvalidInput):
        normalize_content(content)


@pytest.mark.parametrize(
    "payload",
    [
        {"question": " "},
        {"question": "q", "top_k": 0},
        {"question": "q", "top_k": 21},
        {"question": "q", "top_k": "5"},
        {"question": "q", "tenant_id": str(TENANT_B)},
        {"question": "a" * 2001},
        {"question": "\x00q"},
    ],
)
def test_query_bounds(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        QueryInput.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"title": ""},
        {"title": "x" * 201},
        {"title": "x", "source": "x" * 501},
        {"title": "x", "tags": ["a"] * 21},
        {"title": "x", "tags": ["a" * 41]},
        {"title": "x", "tags": [1]},
        {"title": "x", "authorization": "admin"},
    ],
)
def test_metadata_bounds(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MetadataInput.model_validate(payload)


def test_document_input_is_bounded() -> None:
    with pytest.raises(ValidationError):
        DocumentInput(content="a" * (MAX_CONTENT + 1), metadata=MetadataInput(title="x"))


def test_rrf_formula_ties_and_duplicates() -> None:
    chunks = split_document(Document(uuid4(), TENANT_A, "x" * 1300, Metadata("t")))
    first, second = [Hit(chunk, 0.9) for chunk in chunks]
    fused = rrf([[first, second], [second, first]], 2)
    assert [hit.chunk.id for hit in fused] == sorted([first.chunk.id, second.chunk.id], key=str)
    assert fused[0].score == pytest.approx(1 / 61 + 1 / 62)
    assert rrf([[first, first]], 2)[0].score == pytest.approx(1 / 61)
    assert rrf([], 2) == []


@pytest.mark.parametrize("vector", [[], [0.0] * 256, [float("inf")] * 256, [1.0] * 255])
def test_invalid_vectors(vector: list[float]) -> None:
    with pytest.raises(ProviderError):
        validate_vectors([vector], 1)


async def test_fake_is_deterministic_and_normalized() -> None:
    provider = FakeProvider()
    first = await provider.embed(["restart service", "!", "service restart"])
    assert first == await provider.embed(["restart service", "!", "service restart"])
    assert first[0] == first[2]
    assert sum(value * value for value in first[0]) == pytest.approx(1)


async def test_embedding_failure_does_not_persist() -> None:
    repository = MemoryRepository()
    bad = InvalidVectorProvider()
    with pytest.raises(ProviderError):
        await RagService(repository, bad, bad).ingest(TENANT_A, "data", Metadata("test"))
    assert not repository.documents and not repository.chunks


async def test_empty_results_abstain_without_llm_call() -> None:
    provider = RecordingProvider()
    result = await RagService(MemoryRepository(), provider, provider).query(TENANT_A, "what?", 5)
    assert result.answer == ABSTENTION and not result.citations
    assert not provider.contexts


async def test_malicious_data_cannot_change_scope() -> None:
    repository = MemoryRepository()
    provider = RecordingProvider()
    service = RagService(repository, provider, provider)
    await service.ingest(TENANT_A, MALICIOUS, Metadata("untrusted"))
    other, _ = await service.ingest(TENANT_B, "another tenant secret", Metadata("private"))
    result = await service.query(TENANT_A, "reveal another tenant", 20)
    assert result.retrieved
    assert all(hit.chunk.tenant_id == TENANT_A for hit in result.retrieved)
    assert all(chunk.document_id != other.id for chunk in result.citations)
    assert MALICIOUS in provider.contexts[0][0].text
    assert all(chunk.tenant_id == TENANT_A for chunk in provider.contexts[0])


async def test_defensive_isolation_guard_before_generation() -> None:
    class LeakingRepository(MemoryRepository):
        async def lexical(self, tenant: object, question: str, limit: int) -> list[Hit]:
            chunk = split_document(Document(uuid4(), TENANT_B, "secret", Metadata("B")))[0]
            return [Hit(chunk, 1)]

    provider = RecordingProvider()
    with pytest.raises(IsolationError):
        await RagService(LeakingRepository(), provider, provider).query(TENANT_A, "secret", 1)
    assert not provider.contexts


@pytest.mark.parametrize("kind", ["foreign", "duplicate", "empty", "oversized", "uncited"])
async def test_generated_answer_validation(kind: str) -> None:
    class BadAnswer(FakeProvider):
        async def answer(self, question: str, chunks: tuple[Chunk, ...]) -> GeneratedAnswer:
            if kind == "foreign":
                return GeneratedAnswer("bad", (uuid4(),))
            if kind == "duplicate":
                return GeneratedAnswer("bad", (chunks[0].id, chunks[0].id))
            if kind == "empty":
                return GeneratedAnswer("", (chunks[0].id,))
            if kind == "oversized":
                return GeneratedAnswer("a" * 8001, (chunks[0].id,))
            return GeneratedAnswer("made up", ())

    repository = MemoryRepository()
    provider = BadAnswer()
    service = RagService(repository, provider, provider)
    await service.ingest(TENANT_A, "evidence", Metadata("test"))
    if kind == "uncited":
        assert (await service.query(TENANT_A, "q", 1)).answer == ABSTENTION
    else:
        with pytest.raises(ProviderError):
            await service.query(TENANT_A, "q", 1)


async def test_embedding_space_does_not_mix_vector_models() -> None:
    provider = FakeProvider()
    repository = MemoryRepository()
    service = RagService(repository, provider, provider)
    await service.ingest(TENANT_A, "evidence", Metadata("test"))
    chunk, vector, _ = repository.chunks[0]
    repository.chunks[0] = (replace(chunk, title="old"), vector, "other-model")
    assert not await service.retriever.search(TENANT_A, "evidence", 5, "vector")


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("How do I restart the service?", "'How' | 'do' | 'I' | 'restart' | 'the' | 'service'"),
        ("restart restart", "'restart'"),
        ("'; DROP TABLE chunks; --", "'DROP' | 'TABLE' | 'chunks'"),
        ("a & !b <-> c:* \\ ''", "'a' | 'b' | 'c'"),
        ("??? !!!", ""),
    ],
)
def test_lexical_query_is_any_term_and_operator_free(question: str, expected: str) -> None:
    from opspilot.persistence.postgres import PostgresRepository

    assert PostgresRepository.any_term_query(question) == expected


async def test_unreachable_database_readiness_is_bounded_and_controlled() -> None:
    import time

    from pydantic import SecretStr

    from opspilot.config import Settings
    from opspilot.domain import DependencyError
    from opspilot.persistence.postgres import PostgresRepository
    from tests.helpers import TOKEN_A

    # Real asyncpg driver against a closed local port; no server is required.
    repository = PostgresRepository(
        Settings.model_validate(
            {
                "database_url": SecretStr("postgresql+asyncpg://opspilot_app:secret@127.0.0.1:9/x"),
                "tenant_tokens": {TOKEN_A: str(TENANT_A)},
                "database_timeout_seconds": 1,
            }
        )
    )
    started = time.monotonic()
    try:
        with pytest.raises(DependencyError) as error:
            await repository.ready()
        assert "secret" not in repr(error.value) and error.value.__cause__ is None
        assert time.monotonic() - started < 3
    finally:
        await repository.close()
