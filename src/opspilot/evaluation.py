"""Versioned real-retrieval evaluation, never calls the answer model."""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text

from opspilot.api.contracts import MetadataInput
from opspilot.application import RagService
from opspilot.config import Settings
from opspilot.domain import Metadata
from opspilot.persistence.postgres import PostgresRepository
from opspilot.providers.fake import FakeProvider
from opspilot.providers.openai import OpenAIProvider
from opspilot.retrieval import RetrievalMode


class CorpusDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    tenant_id: UUID
    content: str = Field(min_length=1, max_length=100_000)
    metadata: MetadataInput


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    tenant_id: UUID
    question: str = Field(min_length=1, max_length=2000)
    relevant_ids: list[UUID] = Field(min_length=1)
    granularity: Literal["document", "chunk"] = "document"


class Dataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    corpus: list[CorpusDocument] = Field(min_length=1)
    cases: list[EvalCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_relevance(self) -> Self:
        from opspilot.chunking import normalize_content, split_document
        from opspilot.domain import Document

        if len({document.id for document in self.corpus}) != len(self.corpus):
            raise ValueError("duplicate corpus IDs")
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate case IDs")
        document_tenants: dict[UUID, UUID] = {}
        chunk_tenants: dict[UUID, UUID] = {}
        for document in self.corpus:
            document_tenants[document.id] = document.tenant_id
            domain = Document(
                document.id,
                document.tenant_id,
                normalize_content(document.content),
                Metadata("eval"),
            )
            for chunk in split_document(domain):
                chunk_tenants[chunk.id] = document.tenant_id
        for case in self.cases:
            tenants = document_tenants if case.granularity == "document" else chunk_tenants
            if any(tenants.get(relevant) != case.tenant_id for relevant in case.relevant_ids):
                raise ValueError("relevance IDs must exist and belong to the case tenant")
        return self


def metrics(retrieved: list[UUID], relevant: list[UUID], k: int) -> tuple[float, float]:
    if k < 1 or not relevant:
        raise ValueError("k and relevance must be nonempty")
    ranked = list(dict.fromkeys(retrieved))[:k]
    expected = set(relevant)
    recall = len(expected.intersection(ranked)) / len(expected)
    reciprocal_rank = next(
        (1.0 / rank for rank, item in enumerate(ranked, 1) if item in expected), 0.0
    )
    return recall, reciprocal_rank


async def prepare_corpus(
    service: RagService, repository: PostgresRepository, corpus: list[CorpusDocument], seed: bool
) -> None:
    from opspilot.chunking import normalize_content

    for document in corpus:
        async with repository.transaction(document.tenant_id) as connection:
            result = await connection.execute(
                text(
                    "SELECT content, embedding_space, title, source, tags "
                    "FROM documents WHERE id=:id"
                ),
                {"id": document.id},
            )
            existing = result.mappings().one_or_none()
        if existing is not None:
            if (
                existing["content"] != normalize_content(document.content)
                or existing["embedding_space"] != service.embedder.space
                or existing["title"] != document.metadata.title
                or existing["source"] != document.metadata.source
                or existing["tags"] != document.metadata.tags
            ):
                raise ValueError("corpus/provider mismatch; use a fresh evaluation database")
            continue
        if not seed:
            raise ValueError("missing corpus: run with --seed against a dedicated database")
        await service.ingest(
            document.tenant_id,
            document.content,
            Metadata(
                document.metadata.title, document.metadata.source, tuple(document.metadata.tags)
            ),
            document.id,
        )


async def evaluate(dataset_path: Path, k: int, seed: bool) -> dict[str, object]:
    encoded_dataset = await asyncio.to_thread(dataset_path.read_text)
    dataset = Dataset.model_validate_json(encoded_dataset)
    config = Settings.from_env()
    repository = PostgresRepository(config)
    provider = OpenAIProvider(config) if config.provider == "openai" else FakeProvider()
    try:
        await repository.ready()
        service = RagService(repository, provider, provider)
        await prepare_corpus(service, repository, dataset.corpus, seed)
        modes: tuple[RetrievalMode, ...] = ("lexical", "vector", "hybrid")
        output: dict[str, object] = {}
        for mode in modes:
            recalls: list[float] = []
            ranks: list[float] = []
            cases: list[dict[str, object]] = []
            for case in dataset.cases:
                hits = await service.retriever.search(case.tenant_id, case.question, k, mode)
                ids = [
                    hit.chunk.id if case.granularity == "chunk" else hit.chunk.document_id
                    for hit in hits
                ]
                recall, mrr = metrics(ids, case.relevant_ids, k)
                recalls.append(recall)
                ranks.append(mrr)
                cases.append(
                    {
                        "id": case.id,
                        "recall_at_k": recall,
                        "mrr_at_k": mrr,
                        "retrieved_ids": [str(item) for item in ids],
                    }
                )
            output[mode] = {
                "recall_at_k": sum(recalls) / len(recalls),
                "mrr_at_k": sum(ranks) / len(ranks),
                "cases": cases,
            }
        return {
            "dataset_version": dataset.version,
            "embedding_space": provider.space,
            "k": k,
            "case_count": len(dataset.cases),
            "modes": output,
        }
    finally:
        try:
            if isinstance(provider, OpenAIProvider):
                await provider.close()
        finally:
            await repository.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run retrieval evaluation against PostgreSQL")
    parser.add_argument("--dataset", type=Path, default=Path("evals/retrieval-v1.json"))
    parser.add_argument("--k", type=int, choices=range(1, 21), default=5)
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = asyncio.run(evaluate(args.dataset, args.k, args.seed))
    except Exception:
        raise SystemExit("Evaluation failed; verify configuration, corpus and database.") from None
    encoded = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n")
    print(encoded)


if __name__ == "__main__":
    main()
