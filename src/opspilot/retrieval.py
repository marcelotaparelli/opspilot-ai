"""Tenant-scoped retrieval and deterministic reciprocal rank fusion."""

import time
from typing import Literal
from uuid import UUID

from opspilot.domain import Embedder, Hit, IsolationError, Repository, validate_vectors
from opspilot.observability import annotate, count, observe, span

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
        started = time.monotonic()
        candidates = candidate_count(k)
        with span(
            "retrieval",
            ai__retrieval__strategy=mode,
            ai__retrieval__top_k=k,
            ai__retrieval__candidates_requested=candidates,
        ) as current:
            rankings: list[list[Hit]] = []
            if mode in ("vector", "hybrid"):
                vectors = await self.embedder.embed([question])
                validate_vectors(vectors, 1)
                with span("vector_retrieval") as branch:
                    ranking = await self.repository.vector(
                        tenant, vectors[0], self.embedder.space, candidates
                    )
                    annotate(branch, ai__retrieval__candidates_returned=len(ranking))
                rankings.append(ranking)
            if mode in ("lexical", "hybrid"):
                with span("lexical_retrieval") as branch:
                    ranking = await self.repository.lexical(tenant, question, candidates)
                    annotate(branch, ai__retrieval__candidates_returned=len(ranking))
                rankings.append(ranking)
            # Check all candidates, even those which would lose the final ranking.
            if any(hit.chunk.tenant_id != tenant for ranking in rankings for hit in ranking):
                raise IsolationError
            if mode == "hybrid":
                with span("rank_fusion", ai__retrieval__inputs=len(rankings)) as fusion:
                    hits = rrf(rankings, k)
                    annotate(fusion, ai__retrieval__chunks_returned=len(hits))
            else:
                hits = rankings[0][:k]
            annotate(current, ai__retrieval__chunks_returned=len(hits))
        count("retrieval_requests_total", strategy=mode)
        observe("retrieval_duration_seconds", time.monotonic() - started, strategy=mode)
        observe("retrieval_chunks_returned", len(hits), strategy=mode)
        if not hits:
            count("retrieval_empty_total", strategy=mode)
        return hits
