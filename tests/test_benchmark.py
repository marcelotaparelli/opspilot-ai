import hashlib
import json
import math
import re
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from opspilot.benchmark import (
    DEFAULT_DIR,
    Corpus,
    Layout,
    QuerySet,
    check_authoring_lock,
    leakage_issues,
    load,
    ndcg_at,
    recall_at,
    reciprocal_rank_at,
    run,
    validate_labels,
)
from opspilot.chunking import split_document
from opspilot.domain import Document, Hit, Metadata
from opspilot.persistence.postgres import PostgresRepository
from opspilot.retrieval import Retriever
from tests.helpers import TENANT_A, TOKEN_A, MemoryRepository

GRADES = {"a": 2, "b": 1, "c": 1}


def test_metric_definitions_by_hand() -> None:
    ranked = ["x", "b", "a", "y"]
    assert recall_at(ranked, GRADES, 1) == 0
    assert recall_at(ranked, GRADES, 3) == pytest.approx(2 / 3)
    # Denominator is the number of relevant items, not the number retrieved.
    assert recall_at(ranked, GRADES, 5) == pytest.approx(2 / 3)
    assert recall_at(["a"], GRADES, 5) == pytest.approx(1 / 3)
    assert reciprocal_rank_at(ranked, GRADES, 5) == 0.5
    assert reciprocal_rank_at(["x", "y"], GRADES, 5) == 0
    assert reciprocal_rank_at(["x", "a"], GRADES, 1) == 0
    # DCG = (2^1-1)/log2(3) + (2^2-1)/log2(4); IDCG = 3/1 + 1/log2(3) + 1/log2(4)
    dcg = 1 / math.log2(3) + 3 / 2
    idcg = 3 + 1 / math.log2(3) + 1 / 2
    assert ndcg_at(ranked, GRADES, 5) == pytest.approx(dcg / idcg)
    assert ndcg_at(["a", "b", "c"], GRADES, 5) == pytest.approx(1)
    assert ndcg_at(["b", "a"], {"a": 2, "b": 1}, 5) < ndcg_at(["a", "b"], {"a": 2, "b": 1}, 5)


def test_dataset_is_large_labelled_and_leak_free() -> None:
    corpus, dev, heldout = load(DEFAULT_DIR)
    acme = [entry for entry in corpus.documents if entry.tenant == "acme"]
    assert 40 <= len(acme) <= 100
    assert len(dev.queries) >= 20 and len(heldout.queries) >= 30
    types = {query.type for query in [*dev.queries, *heldout.queries]}
    assert len(types) == 6
    assert leakage_issues(corpus, dev, heldout) == []
    # Hard negatives exist for most queries and several queries have multiple relevant docs.
    queries = [*dev.queries, *heldout.queries]
    assert sum(1 for query in queries if query.hard_negatives) >= len(queries) // 2
    assert sum(1 for query in queries if len(query.relevance) > 1) >= 10


def test_split_is_the_documented_result_blind_rule() -> None:
    _, dev, heldout = load(DEFAULT_DIR)
    expected_dev: set[str] = set()
    for kind in {query.type for query in dev.queries}:
        group = [q for q in [*dev.queries, *heldout.queries] if q.type == kind]
        pinned = [q.id for q in group if q.pinned_dev]
        rest = sorted(
            (q.id for q in group if not q.pinned_dev),
            key=lambda item: hashlib.sha256(item.encode()).hexdigest(),
        )
        expected_dev.update(pinned + rest[: 4 - len(pinned)])
    assert {query.id for query in dev.queries} == expected_dev
    # The Phase 1 regression question is kept, pinned to dev.
    assert any(q.question == "How do I restart the service?" for q in dev.queries if q.pinned_dev)


def test_corpus_and_heldout_are_unchanged_since_authoring() -> None:
    check_authoring_lock(DEFAULT_DIR)


def test_leakage_detector_catches_copies_and_near_duplicates() -> None:
    corpus, dev, heldout = load(DEFAULT_DIR)
    tampered = heldout.model_copy(deep=True)
    tampered.queries[0].type = "paraphrase"
    tampered.queries[0].question = "the edge gateway keeps per-client request counters"
    tampered.queries[1].question = dev.queries[1].question + " today"
    issues = leakage_issues(corpus, dev, tampered)
    assert any("copies" in issue for issue in issues)
    assert any("near-duplicate" in issue for issue in issues)


def test_labels_must_stay_in_tenant_and_disjoint() -> None:
    corpus, dev, _ = load(DEFAULT_DIR)
    cross = dev.model_copy(deep=True)
    cross.queries[0].relevance = {"globex-rollback": 2}
    with pytest.raises(ValueError, match="query tenant"):
        validate_labels(corpus, cross)
    overlap = dev.model_copy(deep=True)
    first = next(iter(overlap.queries[0].relevance))
    overlap.queries[0].hard_negatives = [first]
    with pytest.raises(ValueError, match="hard negative"):
        validate_labels(corpus, overlap)


async def test_heldout_refused_without_matching_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://opspilot_app:x@127.0.0.1:9/none")
    monkeypatch.setenv("TENANT_TOKENS", json.dumps({TOKEN_A: str(TENANT_A)}))
    monkeypatch.setenv("PROVIDER", "fake")
    with pytest.raises(ValueError, match="require --freeze"):
        await run(DEFAULT_DIR, "heldout", 5, False, None)
    stale = tmp_path / "freeze.json"
    stale.write_text(json.dumps({"fingerprint": {"config": {"k": 5}}}))
    with pytest.raises(ValueError, match="differ from freeze"):
        await run(DEFAULT_DIR, "heldout", 5, False, stale)


async def test_hybrid_fuses_both_branches_not_one() -> None:
    def hit(text: str) -> Hit:
        chunk = split_document(Document(uuid4(), TENANT_A, text, Metadata(text)))[0]
        return Hit(chunk, 1.0)

    vector_only, lexical_only, both = hit("v"), hit("l"), hit("b")

    class Fixed(MemoryRepository):
        async def vector(
            self, tenant: UUID, vector: list[float], space: str, limit: int
        ) -> list[Hit]:
            return [both, vector_only]

        async def lexical(self, tenant: UUID, question: str, limit: int) -> list[Hit]:
            return [both, lexical_only]

    from tests.helpers import RecordingProvider

    hits = await Retriever(Fixed(), RecordingProvider()).search(TENANT_A, "q", 3)
    ids = [h.chunk.id for h in hits]
    assert ids[0] == both.chunk.id
    assert set(ids) == {both.chunk.id, vector_only.chunk.id, lexical_only.chunk.id}
    assert hits[0].score == pytest.approx(2 / 61)


@pytest.mark.integration
async def test_dev_benchmark_regressions_against_real_postgres(
    postgres: tuple[PostgresRepository, UUID, UUID],
) -> None:
    from opspilot.application import RagService
    from opspilot.benchmark import corpus_documents, evaluate
    from opspilot.evaluation import prepare_corpus
    from opspilot.providers.fake import FakeProvider

    repository, a, b = postgres
    corpus = Corpus.model_validate_json((DEFAULT_DIR / "corpus.json").read_text())
    dev = QuerySet.model_validate_json((DEFAULT_DIR / "dev.json").read_text())
    # Only the dev split is ever executed by tests; held-out stays unseen.
    layout = Layout({"acme": a, "globex": b}, namespace=f"test-{uuid4()}")
    provider = FakeProvider()
    service = RagService(repository, provider, provider)
    await prepare_corpus(service, repository, corpus_documents(corpus, layout), True)
    first = await evaluate(service, repository, corpus, dev, layout, 5)
    assert first == await evaluate(service, repository, corpus, dev, layout, 5)

    queries = first["queries"]
    strategies = first["strategies"]
    diagnostics = first["hybrid_diagnostics"]
    assert isinstance(queries, list) and isinstance(strategies, dict)
    assert isinstance(diagnostics, dict)
    globex = {entry.key for entry in corpus.documents if entry.tenant == "globex"}
    # Letters/digits only, splitting on "_" like the PostgreSQL parser does.
    vocabulary = {
        word
        for entry in corpus.documents
        if entry.tenant == "acme"
        for word in re.findall(r"[^\W_]+", entry.content.casefold())
    }
    for row in queries:
        for mode in ("lexical", "vector", "hybrid"):
            assert not set(row["results"][mode]["ranked"]) & globex
        # Lexical must not be empty when the question shares a whole word with the corpus.
        words = set(re.findall(r"[^\W_]+", row["question"].casefold()))
        if words & vocabulary:
            assert row["lexical_candidates"] > 0, row["id"]
    # Hybrid consumes both branches (RRF recomputed inside evaluate) and does not collapse.
    assert diagnostics["rrf_recomputation_mismatches"] == 0
    assert diagnostics["hybrid_identical_to_vector"] <= 2
    assert diagnostics["hybrid_identical_to_lexical"] <= 2
    # Regression floors set well below measured dev values; an inverted ORDER BY, an empty
    # lexical branch or broken fusion falls far below them.
    floors = {"lexical": 0.5, "vector": 0.25, "hybrid": 0.4}
    for mode, floor in floors.items():
        assert strategies[mode]["overall"]["mrr@5"] >= floor, mode
    # Phase 1 regression: a natural-language question must work lexically without copying.
    # (Hybrid misses it on dev with the fake embedder; documented in retrieval-v2.md.)
    restart = next(row for row in queries if row["question"] == "How do I restart the service?")
    assert restart["results"]["lexical"]["ranked"][0] == "service-restart-procedure"
