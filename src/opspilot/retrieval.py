"""Tenant-scoped retrieval and deterministic reciprocal rank fusion."""

from typing import Literal
from uuid import UUID

from opspilot.domain import Embedder, Hit, IsolationError, Repository, validate_vectors
from opspilot.observability import span

RetrievalMode = Literal["lexical", "vector", "hybrid"]
# 60 is the value proposed with RRF (Cormack, Clarke & Buettcher, SIGIR 2009); a literature
# default, not tuned here. Recorded in benchmark freeze manifests together with the depth.
RRF_CONSTANT = 60
MAX_CANDIDATES = 80


def candidate_count(k: int) -> int:
    """Candidates fetched per branch before fusion: an untuned bounded heuristic."""
    return min(k * 4, MAX_CANDIDATES)


def rrf(rankings: list[list[Hit]], k: int, constant: int = RRF_CONSTANT) -> list[Hit]:
    # score = sum(1 / (constant + one-based rank)); duplicates within a ranking count once.
    scores: dict[UUID, float] = {}
    chunks = {hit.chunk.id: hit.chunk for ranking in rankings for hit in ranking}
    for ranking in rankings:
        seen: set[UUID] = set()
        for rank, hit in enumerate(ranking, start=1):
            if hit.chunk.id not in seen:
                scores[hit.chunk.id] = scores.get(hit.chunk.id, 0.0) + 1.0 / (constant + rank)
                seen.add(hit.chunk.id)
    ids = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], str(chunk_id)))[:k]
    return [Hit(chunks[chunk_id], scores[chunk_id]) for chunk_id in ids]


class Retriever:
    def __init__(self, repository: Repository, embedder: Embedder) -> None:
        self.repository = repository
        self.embedder = embedder

    async def search(
        self, tenant: UUID, question: str, k: int, mode: RetrievalMode = "hybrid"
    ) -> list[Hit]:
        with span("retrieval"):
            candidates = candidate_count(k)
            rankings: list[list[Hit]] = []
            if mode in ("vector", "hybrid"):
                with span("embedding"):
                    vectors = await self.embedder.embed([question])
                    validate_vectors(vectors, 1)
                rankings.append(
                    await self.repository.vector(
                        tenant, vectors[0], self.embedder.space, candidates
                    )
                )
            if mode in ("lexical", "hybrid"):
                rankings.append(await self.repository.lexical(tenant, question, candidates))
            # Check all candidates, even those which would lose the final ranking.
            if any(hit.chunk.tenant_id != tenant for ranking in rankings for hit in ranking):
                raise IsolationError
            return rrf(rankings, k) if mode == "hybrid" else rankings[0][:k]
