import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from opspilot.evaluation import Dataset, metrics
from opspilot.persistence.postgres import PostgresRepository
from opspilot.retrieval import RetrievalMode


def test_measured_metric_definitions() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    assert metrics([c, a, b], [a, b], 2) == (0.5, 0.5)
    assert metrics([c, a, b], [a, b], 3) == (1.0, 0.5)
    assert metrics([], [a], 5) == (0.0, 0.0)
    assert metrics([c, c, a], [a], 2) == (1.0, 0.5)
    with pytest.raises(ValueError):
        metrics([a], [], 5)


def test_versioned_dataset_relevance_is_resolvable() -> None:
    dataset = Dataset.model_validate_json(Path("evals/retrieval-v1.json").read_text())
    assert dataset.version == 1 and len(dataset.cases) >= 3


def test_dataset_rejects_cross_tenant_relevance() -> None:
    from pydantic import ValidationError

    dataset = Dataset.model_validate_json(Path("evals/retrieval-v1.json").read_text())
    data = dataset.model_dump()
    data["cases"][0]["relevant_ids"] = [dataset.corpus[-1].id]
    with pytest.raises(ValidationError):
        Dataset.model_validate(data)


@pytest.mark.integration
async def test_full_harness_seed_and_repeatability(
    postgres: tuple[PostgresRepository, UUID, UUID],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from opspilot.chunking import normalize_content, split_document
    from opspilot.domain import Document, Metadata
    from opspilot.evaluation import evaluate
    from tests.helpers import TOKEN_A

    _, a, b = postgres
    encoded = await asyncio.to_thread(Path("evals/retrieval-v1.json").read_text)
    dataset = Dataset.model_validate_json(encoded)
    data = dataset.model_dump(mode="json")
    id_mapping: dict[str, str] = {}
    for index, document in enumerate(data["corpus"]):
        old = dataset.corpus[index]
        new_id = uuid4()
        tenant = b if index == len(dataset.corpus) - 1 else a
        document["id"], document["tenant_id"] = str(new_id), str(tenant)
        old_chunks = split_document(
            Document(old.id, old.tenant_id, normalize_content(old.content), Metadata("eval"))
        )
        new_chunks = split_document(
            Document(new_id, tenant, normalize_content(old.content), Metadata("eval"))
        )
        id_mapping[str(old.id)] = str(new_id)
        for old_chunk, new_chunk in zip(old_chunks, new_chunks, strict=True):
            id_mapping[str(old_chunk.id)] = str(new_chunk.id)
    for case in data["cases"]:
        case["tenant_id"] = str(a)
        case["relevant_ids"] = [id_mapping[item] for item in case["relevant_ids"]]
    path = tmp_path / "dataset.json"
    await asyncio.to_thread(path.write_text, Dataset.model_validate(data).model_dump_json())
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setenv("TENANT_TOKENS", '{"' + TOKEN_A + '":"' + str(a) + '"}')
    monkeypatch.setenv("PROVIDER", "fake")
    first = await evaluate(path, 5, True)
    assert first == await evaluate(path, 5, True)
    assert first == await evaluate(path, 5, False)
    modes = first["modes"]
    assert isinstance(modes, dict) and set(modes) == {"lexical", "vector", "hybrid"}


@pytest.mark.integration
async def test_real_evaluation_harness_modes(
    postgres: tuple[PostgresRepository, UUID, UUID],
) -> None:
    from opspilot.application import RagService
    from opspilot.domain import Metadata
    from opspilot.providers.fake import FakeProvider

    repository, tenant, _ = postgres
    provider = FakeProvider()
    service = RagService(repository, provider, provider)
    document, _ = await service.ingest(tenant, "rotate certificate before expiry", Metadata("cert"))
    modes: tuple[RetrievalMode, ...] = ("lexical", "vector", "hybrid")
    for mode in modes:
        hits = await service.retriever.search(tenant, "rotate certificate", 5, mode)
        recall, mrr = metrics([hit.chunk.document_id for hit in hits], [document.id], 5)
        assert recall == 1 and mrr == 1
