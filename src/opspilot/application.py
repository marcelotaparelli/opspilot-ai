"""Ingest/query use cases depend on ports and plain domain values only."""

from uuid import UUID, uuid4

from opspilot.chunking import normalize_content, split_document
from opspilot.domain import (
    ABSTENTION,
    Answerer,
    Document,
    Embedder,
    Metadata,
    ProviderError,
    QueryResult,
    Repository,
    validate_vectors,
)
from opspilot.observability import span
from opspilot.retrieval import Retriever


class RagService:
    def __init__(self, repository: Repository, embedder: Embedder, answerer: Answerer) -> None:
        self.repository = repository
        self.embedder = embedder
        self.answerer = answerer
        self.retriever = Retriever(repository, embedder)

    async def ingest(
        self, tenant: UUID, content: str, metadata: Metadata, document_id: UUID | None = None
    ) -> tuple[Document, list[UUID]]:
        document = Document(document_id or uuid4(), tenant, normalize_content(content), metadata)
        chunks = split_document(document)
        vectors: list[list[float]] = []
        with span("embedding"):
            # Bounded requests: at most 16 windows / 19,200 characters per call.
            for offset in range(0, len(chunks), 16):
                batch = chunks[offset : offset + 16]
                embedded = await self.embedder.embed([chunk.text for chunk in batch])
                validate_vectors(embedded, len(batch))
                vectors.extend(embedded)
        # One database transaction, after all external calls succeed.
        await self.repository.save(document, chunks, vectors, self.embedder.space)
        return document, [chunk.id for chunk in chunks]

    async def query(self, tenant: UUID, question: str, k: int) -> QueryResult:
        hits = await self.retriever.search(tenant, question, k)
        if not hits:
            return QueryResult(ABSTENTION, (), ())
        # Maximum 24,000 context characters, enforced by chunk and k bounds.
        context = tuple(hit.chunk for hit in hits)
        with span("llm"):
            generated = await self.answerer.answer(question, context)
        allowed = {chunk.id: chunk for chunk in context}
        if (
            not generated.answer.strip()
            or len(generated.answer) > 8000
            or len(generated.cited_chunk_ids) > len(context)
            or any(chunk_id not in allowed for chunk_id in generated.cited_chunk_ids)
            or len(set(generated.cited_chunk_ids)) != len(generated.cited_chunk_ids)
        ):
            raise ProviderError
        # Uncited output cannot become an asserted answer. Fail closed to abstention.
        if not generated.cited_chunk_ids:
            return QueryResult(ABSTENTION, (), tuple(hits))
        citations = tuple(allowed[chunk_id] for chunk_id in generated.cited_chunk_ids)
        return QueryResult(generated.answer, citations, tuple(hits))
