"""retrieval-v2: graded labels, dev/held-out split and freeze-verified held-out runs.

Ground truth is hand-written in evals/retrieval-v2 and never derived from a retriever.
The answer model is never called. A held-out run refuses to start unless the dataset,
retrieval source code and configuration match a previously written freeze manifest.
"""

import argparse
import asyncio
import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

import opspilot
from opspilot.api.contracts import MetadataInput
from opspilot.application import RagService
from opspilot.chunking import CHUNK_SIZE, OVERLAP, normalize_content, split_document
from opspilot.config import Settings
from opspilot.domain import EMBEDDING_SPACE_FAKE, Document, Hit, Metadata
from opspilot.evaluation import CorpusDocument, prepare_corpus
from opspilot.persistence.postgres import PostgresRepository
from opspilot.providers.fake import FakeProvider
from opspilot.providers.openai import OpenAIProvider
from opspilot.retrieval import RRF_CONSTANT, RetrievalMode, candidate_count, rrf

QueryType = Literal[
    "paraphrase", "explicit_terms", "identifier", "ambiguous", "multi_relevant", "hard_negative"
]
Split = Literal["dev", "heldout"]
MODES: tuple[RetrievalMode, ...] = ("lexical", "vector", "hybrid")
CUTOFFS = (1, 3, 5)
DEFAULT_DIR = Path("evals/retrieval-v2")
ID_NAMESPACE = "opspilot-eval:retrieval-v2"
# Files whose behaviour determines the measured numbers; a held-out run requires them unchanged.
FROZEN_SOURCES = (
    "application.py",
    "benchmark.py",
    "chunking.py",
    "domain.py",
    "evaluation.py",
    "persistence/postgres.py",
    "persistence/schema.sql",
    "providers/fake.py",
    "retrieval.py",
)
WORD = re.compile(r"\w+")
SHINGLE = 5
NEAR_DUPLICATE_JACCARD = 0.5


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-z0-9-]{1,80}$")
    tenant: str
    title: str
    tags: list[str] = Field(default_factory=list)
    content: str = Field(min_length=1, max_length=100_000)


class Corpus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[2]
    name: str
    description: str
    tenants: dict[str, UUID] = Field(min_length=1)
    documents: list[Entry] = Field(min_length=1)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if len({entry.key for entry in self.documents}) != len(self.documents):
            raise ValueError("duplicate document keys")
        if any(entry.tenant not in self.tenants for entry in self.documents):
            raise ValueError("unknown tenant")
        return self


class Query(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    tenant: str
    type: QueryType
    question: str = Field(min_length=1, max_length=2000)
    # Graded relevance: 2 = directly answers, 1 = partially relevant or supporting.
    relevance: dict[str, Literal[1, 2]] = Field(min_length=1)
    hard_negatives: list[str] = Field(default_factory=list)
    notes: str = ""
    pinned_dev: bool = False


class QuerySet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[2]
    split: Split
    corpus: str
    queries: list[Query] = Field(min_length=1)


def validate_labels(corpus: Corpus, queries: QuerySet) -> None:
    tenants = {entry.key: entry.tenant for entry in corpus.documents}
    if len({query.id for query in queries.queries}) != len(queries.queries):
        raise ValueError("duplicate query IDs")
    for query in queries.queries:
        labelled = [*query.relevance, *query.hard_negatives]
        if any(tenants.get(key) != query.tenant for key in labelled):
            raise ValueError(f"{query.id}: labels must exist in the query tenant")
        if set(query.relevance) & set(query.hard_negatives):
            raise ValueError(f"{query.id}: a hard negative cannot be relevant")
        if query.pinned_dev and queries.split != "dev":
            raise ValueError(f"{query.id}: pinned queries belong to dev")


def words(text: str) -> list[str]:
    return WORD.findall(text.casefold())


def leakage_issues(corpus: Corpus, dev: QuerySet, heldout: QuerySet) -> list[str]:
    """Result-blind checks: no retriever output is consulted."""
    issues: list[str] = []
    shingles: set[tuple[str, ...]] = set()
    for entry in corpus.documents:
        tokens = words(entry.content)
        shingles.update(tuple(tokens[i : i + SHINGLE]) for i in range(len(tokens) - SHINGLE + 1))
    for query in [*dev.queries, *heldout.queries]:
        tokens = words(query.question)
        copied = [tuple(tokens[i : i + SHINGLE]) for i in range(len(tokens) - SHINGLE + 1)]
        # Identifier queries are exact strings by design; every other type must not copy text.
        if query.type != "identifier" and any(item in shingles for item in copied):
            issues.append(f"{query.id}: copies a {SHINGLE}-word sequence from the corpus")
    if {q.id for q in dev.queries} & {q.id for q in heldout.queries}:
        issues.append("query IDs shared between splits")
    for left in dev.queries:
        for right in heldout.queries:
            a, b = set(words(left.question)), set(words(right.question))
            if len(a & b) / len(a | b) >= NEAR_DUPLICATE_JACCARD:
                issues.append(f"{left.id} / {right.id}: near-duplicate questions across splits")
    return issues


def recall_at(ranked: list[str], grades: dict[str, int], k: int) -> float:
    return len(set(ranked[:k]) & set(grades)) / len(grades)


def reciprocal_rank_at(ranked: list[str], grades: dict[str, int], k: int) -> float:
    return next((1.0 / rank for rank, key in enumerate(ranked[:k], 1) if key in grades), 0.0)


def ndcg_at(ranked: list[str], grades: dict[str, int], k: int) -> float:
    """Exponential gain 2^grade - 1, log2 discount; ideal ranking from all labels."""

    def dcg(gains: Iterable[int]) -> float:
        return sum((2.0**gain - 1) / math.log2(rank + 1) for rank, gain in enumerate(gains, 1))

    ideal = dcg(sorted(grades.values(), reverse=True)[:k])
    return dcg(grades.get(key, 0) for key in ranked[:k]) / ideal


@dataclass(frozen=True)
class Layout:
    """Maps logical tenants/keys to UUIDs; tests use their own tenants and namespace."""

    tenants: dict[str, UUID]
    namespace: str = ID_NAMESPACE

    def document_id(self, key: str) -> UUID:
        return uuid5(NAMESPACE_URL, f"{self.namespace}:{key}")


def corpus_documents(corpus: Corpus, layout: Layout) -> list[CorpusDocument]:
    return [
        CorpusDocument(
            id=layout.document_id(entry.key),
            tenant_id=layout.tenants[entry.tenant],
            content=entry.content,
            metadata=MetadataInput(title=entry.title, tags=entry.tags),
        )
        for entry in corpus.documents
    ]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint(dataset_dir: Path, k: int, embedding_space: str) -> dict[str, object]:
    package = Path(opspilot.__file__).parent
    return {
        "dataset": {
            name: sha256_file(dataset_dir / name)
            for name in ("corpus.json", "dev.json", "heldout.json")
        },
        "sources": {name: sha256_file(package / name) for name in FROZEN_SOURCES},
        "config": {
            "k": k,
            "candidates_per_branch": candidate_count(k),
            "rrf_constant": RRF_CONSTANT,
            "chunk_size": CHUNK_SIZE,
            "chunk_overlap": OVERLAP,
            "embedding_space": embedding_space,
            "lexical": "to_tsquery('simple') OR of question word tokens, ts_rank",
            "vector": "exact cosine distance (pgvector <=>)",
            "metrics": "document level; Recall@1/3/5, MRR@5, NDCG@5 (gain 2^g-1)",
        },
    }


def check_authoring_lock(dataset_dir: Path) -> None:
    """Corpus and held-out queries must be byte-identical to the authoring-time record."""
    log = json.loads((dataset_dir / "authoring-log.json").read_text())
    for name in ("corpus.json", "heldout.json"):
        if log["sha256"][name] != sha256_file(dataset_dir / name):
            raise ValueError(f"{name} changed after authoring; held-out results would be invalid")


def dataset_statistics(corpus: Corpus, queries: QuerySet, layout: Layout) -> dict[str, object]:
    chunks: Counter[str] = Counter()
    for entry in corpus.documents:
        document = Document(
            layout.document_id(entry.key),
            layout.tenants[entry.tenant],
            normalize_content(entry.content),
            Metadata(entry.title),
        )
        chunks[entry.tenant] += len(split_document(document))
    texts = {entry.key: set(words(entry.content)) for entry in corpus.documents}
    overlap: dict[str, list[float]] = {}
    for query in queries.queries:
        tokens = set(words(query.question))
        best = set().union(*(texts[key] for key, grade in query.relevance.items() if grade == 2))
        overlap.setdefault(query.type, []).append(len(tokens & best) / len(tokens) if best else 0)
    count = len(queries.queries)
    return {
        "queries": count,
        "queries_by_type": dict(Counter(query.type for query in queries.queries)),
        "corpus_documents": dict(Counter(entry.tenant for entry in corpus.documents)),
        "corpus_chunks": dict(chunks),
        "relevant_per_query_mean": sum(len(q.relevance) for q in queries.queries) / count,
        "grade2_per_query_mean": sum(
            sum(1 for grade in q.relevance.values() if grade == 2) for q in queries.queries
        )
        / count,
        "queries_with_hard_negatives": sum(1 for q in queries.queries if q.hard_negatives),
        "question_word_overlap_with_grade2_documents_by_type": {
            kind: sum(values) / len(values) for kind, values in sorted(overlap.items())
        },
    }


def mean(values: list[float]) -> float:
    return sum(values) / len(values)


def summarise(rows: list[dict[str, float]]) -> dict[str, float]:
    return {name: mean([row[name] for row in rows]) for name in rows[0]} | {"n": len(rows)}


async def evaluate(
    service: RagService,
    repository: PostgresRepository,
    corpus: Corpus,
    queries: QuerySet,
    layout: Layout,
    k: int,
) -> dict[str, object]:
    keys = {layout.document_id(entry.key): entry.key for entry in corpus.documents}
    per_query: list[dict[str, object]] = []
    scores: dict[RetrievalMode, list[tuple[str, dict[str, float]]]] = {mode: [] for mode in MODES}
    diagnostics: Counter[str] = Counter()

    def ranked_keys(hits: list[Hit]) -> list[str]:
        ranked = list(dict.fromkeys(hit.chunk.document_id for hit in hits))
        if any(document not in keys for document in ranked):
            # Results must be attributable to labelled documents; use a dedicated database.
            raise RuntimeError("unlabelled document retrieved: evaluation database is not clean")
        return [keys[document] for document in ranked]

    for query in queries.queries:
        tenant = layout.tenants[query.tenant]
        grades: dict[str, int] = dict(query.relevance)
        results: dict[str, object] = {}
        hits_by_mode: dict[RetrievalMode, list[Hit]] = {}
        for mode in MODES:
            hits = await service.retriever.search(tenant, query.question, k, mode)
            hits_by_mode[mode] = hits
            ranked = ranked_keys(hits)
            row = {f"recall@{cut}": recall_at(ranked, grades, cut) for cut in CUTOFFS}
            row |= {
                "mrr@5": reciprocal_rank_at(ranked, grades, 5),
                "ndcg@5": ndcg_at(ranked, grades, 5),
            }
            relevant_ranks = [rank for rank, key in enumerate(ranked, 1) if key in grades]
            negative_ranks = [
                rank for rank, key in enumerate(ranked, 1) if key in query.hard_negatives
            ]
            outranked = bool(negative_ranks) and (
                not relevant_ranks or min(negative_ranks) < min(relevant_ranks)
            )
            scores[mode].append((query.type, row))
            results[mode] = {"ranked": ranked, "hard_negative_outranks_relevant": outranked} | row
            if query.hard_negatives:
                diagnostics[f"{mode}:hard_negative_queries"] += 1
                diagnostics[f"{mode}:hard_negative_outranks_relevant"] += outranked
        # Independent recomputation: hybrid must be RRF over BOTH branch candidate lists.
        depth = candidate_count(k)
        (embedded,) = await service.embedder.embed([query.question])
        vector = await repository.vector(tenant, embedded, service.embedder.space, depth)
        lexical = await repository.lexical(tenant, query.question, depth)
        fused = [hit.chunk.id for hit in rrf([vector, lexical], k)]
        hybrid = [hit.chunk.id for hit in hits_by_mode["hybrid"]]
        if fused != hybrid:
            raise RuntimeError(f"{query.id}: hybrid output is not RRF of vector and lexical")
        diagnostics["lexical_branch_empty"] += not lexical
        diagnostics["vector_branch_empty"] += not vector
        diagnostics["hybrid_identical_to_vector"] += hybrid == [
            hit.chunk.id for hit in hits_by_mode["vector"]
        ]
        diagnostics["hybrid_identical_to_lexical"] += hybrid == [
            hit.chunk.id for hit in hits_by_mode["lexical"]
        ]
        per_query.append(
            {
                "id": query.id,
                "type": query.type,
                "question": query.question,
                "relevance": query.relevance,
                "hard_negatives": query.hard_negatives,
                "lexical_candidates": len(lexical),
                "vector_candidates": len(vector),
                "results": results,
            }
        )
    strategies: dict[str, object] = {}
    for mode in MODES:
        by_type: dict[str, list[dict[str, float]]] = {}
        for kind, row in scores[mode]:
            by_type.setdefault(kind, []).append(row)
        hard = diagnostics[f"{mode}:hard_negative_queries"]
        strategies[mode] = {
            "overall": summarise([row for _, row in scores[mode]]),
            "by_type": {kind: summarise(rows) for kind, rows in sorted(by_type.items())},
            "queries_without_relevant_in_top5": sum(
                1 for _, row in scores[mode] if row["recall@5"] == 0
            ),
            "hard_negative_outranks_relevant": {
                "queries": diagnostics[f"{mode}:hard_negative_outranks_relevant"],
                "of": hard,
            },
        }
    return {
        "strategies": strategies,
        "hybrid_diagnostics": {
            name: diagnostics[name]
            for name in (
                "lexical_branch_empty",
                "vector_branch_empty",
                "hybrid_identical_to_vector",
                "hybrid_identical_to_lexical",
            )
        }
        | {"queries": len(queries.queries), "rrf_recomputation_mismatches": 0},
        "queries": per_query,
    }


def load(dataset_dir: Path) -> tuple[Corpus, QuerySet, QuerySet]:
    corpus = Corpus.model_validate_json((dataset_dir / "corpus.json").read_text())
    dev = QuerySet.model_validate_json((dataset_dir / "dev.json").read_text())
    heldout = QuerySet.model_validate_json((dataset_dir / "heldout.json").read_text())
    if dev.split != "dev" or heldout.split != "heldout":
        raise ValueError("split files are mislabelled")
    for split in (dev, heldout):
        validate_labels(corpus, split)
    issues = leakage_issues(corpus, dev, heldout)
    if issues:
        raise ValueError("; ".join(issues))
    return corpus, dev, heldout


async def tie_sensitivity(
    service: RagService,
    repository: PostgresRepository,
    corpus: Corpus,
    queries: QuerySet,
    k: int,
    replicates: int,
) -> dict[str, object]:
    """Re-measure under random document UUIDs: UUIDs only break ties, so spread = tie noise."""
    values: dict[str, dict[str, list[float]]] = {}
    for _ in range(replicates):
        layout = Layout({name: uuid4() for name in corpus.tenants}, namespace=f"tie-{uuid4()}")
        await prepare_corpus(service, repository, corpus_documents(corpus, layout), True)
        measured = await evaluate(service, repository, corpus, queries, layout, k)
        strategies = cast(dict[str, dict[str, dict[str, float]]], measured["strategies"])
        for mode, data in strategies.items():
            for metric, value in data["overall"].items():
                if metric != "n":
                    values.setdefault(mode, {}).setdefault(metric, []).append(value)
    return {
        "replicates": replicates,
        "strategies": {
            mode: {
                metric: {"min": min(v), "mean": mean(v), "max": max(v)}
                for metric, v in metrics.items()
            }
            for mode, metrics in values.items()
        },
    }


async def run(
    dataset_dir: Path,
    split: Split,
    k: int,
    seed: bool,
    freeze: Path | None,
    replicates: int = 0,
) -> dict[str, object]:
    corpus, dev, heldout = load(dataset_dir)
    config = Settings.from_env()
    space = config.embedding_space if config.provider == "openai" else EMBEDDING_SPACE_FAKE
    current = fingerprint(dataset_dir, k, space)
    manifest_hash = None
    # All held-out guards run before any database connection is opened.
    if split == "heldout":
        if freeze is None:
            raise ValueError("held-out runs require --freeze")
        check_authoring_lock(dataset_dir)
        manifest = json.loads(freeze.read_text())
        if manifest.get("fingerprint") != current:
            raise ValueError("held-out refused: code, data or configuration differ from freeze")
        manifest_hash = sha256_file(freeze)
    repository = PostgresRepository(config)
    provider = OpenAIProvider(config) if config.provider == "openai" else FakeProvider()
    layout = Layout(corpus.tenants)
    queries = dev if split == "dev" else heldout
    try:
        await repository.ready()
        service = RagService(repository, provider, provider)
        await prepare_corpus(service, repository, corpus_documents(corpus, layout), seed)
        measured = await evaluate(service, repository, corpus, queries, layout, k)
        if replicates:
            measured["tie_sensitivity"] = await tie_sensitivity(
                service, repository, corpus, queries, k, replicates
            )
    finally:
        try:
            if isinstance(provider, OpenAIProvider):
                await provider.close()
        finally:
            await repository.close()
    return {
        "benchmark": corpus.name,
        "split": split,
        "k": k,
        "freeze_manifest_sha256": manifest_hash,
        "fingerprint": current,
        "dataset": dataset_statistics(corpus, queries, layout),
    } | measured


def main() -> None:
    parser = argparse.ArgumentParser(description="retrieval-v2 benchmark")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="labels and leakage checks; no database")
    freeze = commands.add_parser("freeze", help="write the freeze manifest")
    freeze.add_argument("--output", type=Path, required=True)
    freeze.add_argument("--note", default="")
    execute = commands.add_parser("run", help="measure one split against PostgreSQL")
    execute.add_argument("--split", choices=("dev", "heldout"), required=True)
    execute.add_argument("--seed", action="store_true")
    execute.add_argument("--freeze", type=Path)
    execute.add_argument("--output", type=Path)
    execute.add_argument("--tie-replicates", type=int, default=0, choices=range(0, 51))
    for command in (freeze, execute):
        command.add_argument("--k", type=int, choices=range(1, 21), default=5)
    for command in commands.choices.values():
        command.add_argument("--dataset", type=Path, default=DEFAULT_DIR)
    args = parser.parse_args()
    if args.command == "validate":
        corpus, dev, heldout = load(args.dataset)
        counts = {"documents": len(corpus.documents), "dev": len(dev.queries)}
        print(json.dumps(counts | {"heldout": len(heldout.queries), "issues": []}))
        return
    if args.command == "freeze":
        load(args.dataset)
        check_authoring_lock(args.dataset)
        config = Settings.from_env()
        space = config.embedding_space if config.provider == "openai" else EMBEDDING_SPACE_FAKE
        manifest = {"note": args.note, "fingerprint": fingerprint(args.dataset, args.k, space)}
        args.output.write_text(json.dumps(manifest, indent=2) + "\n")
        print(json.dumps(manifest, indent=2))
        return
    result = asyncio.run(
        run(args.dataset, args.split, args.k, args.seed, args.freeze, args.tie_replicates)
    )
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    strategies = cast(dict[str, dict[str, object]], result["strategies"])
    overview = {mode: data["overall"] for mode, data in strategies.items()}
    print(json.dumps({"split": args.split, "strategies": overview}, indent=2))
    print(json.dumps({"hybrid_diagnostics": result["hybrid_diagnostics"]}, indent=2))


if __name__ == "__main__":
    main()
