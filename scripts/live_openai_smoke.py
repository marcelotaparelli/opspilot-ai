"""OPT-IN live smoke against the real OpenAI API through the project's own adapters.

Never part of tests or CI. Runs only when BOTH are set:
    OPENAI_API_KEY=...                      (never printed or persisted)
    OPSPILOT_ALLOW_REAL_OPENAI_SMOKE=true   (explicit spend acknowledgement)
Optional: DATABASE_URL (runtime role) enables the RAG end-to-end step against PostgreSQL;
MODEL_PRICING enables cost estimates; EMBEDDING_MODEL / ANSWER_MODEL override defaults.

Budget: at most MAX_REQUESTS small requests (~a few thousand tokens in total). Proves
integration (models exist, 256-d embeddings, strict schemas accepted, usage reported,
timeouts bounded, no secret in logs/spans). It says nothing about retrieval quality.

    PYTHONPATH=. python -m scripts.live_openai_smoke \
        --output docs/evidence/release/live-openai-smoke.json
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from openai import AsyncOpenAI, OpenAIError
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from opspilot import observability
from opspilot.agent.planners import OpenAIPlanner
from opspilot.agent.ports import PlannerView
from opspilot.application import RagService
from opspilot.chunking import split_document
from opspilot.config import Settings
from opspilot.domain import DIMENSIONS, Document, Metadata, ProviderError
from opspilot.persistence.postgres import PostgresRepository
from opspilot.providers.openai import OpenAIProvider

MAX_REQUESTS = 9
GATE = "OPSPILOT_ALLOW_REAL_OPENAI_SMOKE"
DOCUMENT = "Payments restart runbook: drain traffic, then run a rolling restart of payments-api."
NO_DATABASE_URL = "postgresql+asyncpg://unused:unused@localhost/unused"


def gate_failure(environ: dict[str, str]) -> str | None:
    if not environ.get("OPENAI_API_KEY"):
        return "NOT EXECUTED: OPENAI_API_KEY not provided"
    if environ.get(GATE) != "true":
        return f"NOT EXECUTED: set {GATE}=true to acknowledge paid requests"
    return None


def settings_from(environ: dict[str, str]) -> Settings:
    return Settings.model_validate(
        {
            "database_url": environ.get("DATABASE_URL", NO_DATABASE_URL),
            "tenant_tokens": {"s" * 40: str(uuid4())},
            "provider": "openai",
            "openai_api_key": environ["OPENAI_API_KEY"],
            "embedding_model": environ.get("EMBEDDING_MODEL", "text-embedding-3-small"),
            "answer_model": environ.get("ANSWER_MODEL", "gpt-4.1-mini"),
            "provider_timeout_seconds": 20,
            "agent_llm_timeout_seconds": 20,
            "model_pricing": {"prices": json.loads(environ.get("MODEL_PRICING") or "[]")},
        }
    )


@contextmanager
def capture() -> Iterator[tuple[InMemorySpanExporter, list[str]]]:
    """Spans and every log record (any logger, DEBUG) for the leak check; restored after."""
    observability.setup(None)
    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = Capture(level=logging.DEBUG)
    root, ours = logging.getLogger(), logging.getLogger("opspilot")
    level = root.level
    root.addHandler(handler)
    ours.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield exporter, records
    finally:
        root.removeHandler(handler)
        ours.removeHandler(handler)
        root.setLevel(level)
        exporter.shutdown()


async def run(settings: Settings, client: AsyncOpenAI | None = None) -> dict[str, Any]:
    with capture() as (exporter, records):
        return await probe(settings, client, exporter, records)


async def probe(
    settings: Settings,
    client: AsyncOpenAI | None,
    exporter: InMemorySpanExporter,
    records: list[str],
) -> dict[str, Any]:
    provider = OpenAIProvider(settings, client)
    planner = OpenAIPlanner(settings, provider.client)
    with_database = settings.database_url.get_secret_value() != NO_DATABASE_URL
    steps: list[dict[str, Any]] = []
    requests = 0

    async def step(name: str, action: Callable[[], Awaitable[dict[str, Any]]], calls: int) -> None:
        nonlocal requests
        if requests + calls > MAX_REQUESTS:
            steps.append({"step": name, "status": "skipped_budget"})
            return
        requests += calls
        started = time.monotonic()
        try:
            detail = await action()
            status = "ok"
        except (ProviderError, OpenAIError):
            # Error classes only; provider messages may echo inputs.
            detail, status = {}, "failed"
        steps.append({"step": name, "status": status, "ms": elapsed(started)} | detail)

    chunk = split_document(Document(uuid4(), uuid4(), DOCUMENT, Metadata("runbook")))[0]

    async def models_exist() -> dict[str, Any]:
        found = [(await provider.client.models.retrieve(m)).id for m in settings_models(settings)]
        return {"models_found": found}

    async def embedding() -> dict[str, Any]:
        (vector,) = await provider.embed(["rolling restart of payments-api"])
        return {"dimensions": len(vector), "expected_dimensions": DIMENSIONS}

    async def structured_answer() -> dict[str, Any]:
        # ModelAnswer: strict schema with minLength/maxLength, maxItems and uuid format.
        answer = await provider.answer("How do I restart payments-api?", (chunk,))
        return {
            "schema": "ModelAnswer",
            "schema_accepted": True,
            "citations_within_evidence": set(answer.cited_chunk_ids) <= {chunk.id},
        }

    async def planner_schema() -> dict[str, Any]:
        view = PlannerView(
            "How do I restart payments-api?",
            [{"project": "sandbox", "labels": ["ops"], "assignees": []}],
            [],
            3,
        )
        decision = await planner.decide(view)
        tool = decision.get("tool") if isinstance(decision, dict) else None
        return {"schema": "Envelope(decision union)", "schema_accepted": True, "tool": tool}

    async def rag_end_to_end() -> dict[str, Any]:
        # Throwaway tenant: 1 ingest embedding + 1 query embedding + 1 answer.
        repository = PostgresRepository(settings)
        tenant = uuid4()
        try:
            rag = RagService(repository, provider, provider)
            await rag.ingest(tenant, DOCUMENT, Metadata("runbook"))
            result = await rag.query(tenant, "How do I restart payments-api?", 3)
            return {"retrieved": len(result.retrieved), "citations": len(result.citations)}
        finally:
            await repository.close()

    async def timeout_bounded() -> dict[str, Any]:
        # 0.1 s is the configured minimum; model_copy skips validation to force a sure miss.
        tight_settings = settings.model_copy(update={"provider_timeout_seconds": 0.001})
        tight = OpenAIProvider(tight_settings, client)
        started = time.monotonic()
        try:
            await tight.embed(["timeout probe"])
            return {"timed_out": False}
        except ProviderError:
            return {"timed_out": True, "elapsed_ms": elapsed(started)}
        finally:
            if client is None:
                await tight.close()

    await step("models_exist", models_exist, len(settings_models(settings)))
    await step("embedding", embedding, 1)
    await step("structured_generation", structured_answer, 1)
    await step("planner_structured_output", planner_schema, 1)
    if with_database:
        await step("rag_end_to_end", rag_end_to_end, 3)
    else:
        steps.append({"step": "rag_end_to_end", "status": "skipped_no_database"})
    await step("timeout", timeout_bounded, 1)
    await provider.close()

    spans = exporter.get_finished_spans()
    usage = [
        {
            "operation": s.attributes.get("ai.operation"),
            "configured_model": s.attributes.get("ai.model"),
            "served_model": s.attributes.get("ai.response.model"),
            "input_tokens": s.attributes.get("ai.usage.input_tokens"),
            "output_tokens": s.attributes.get("ai.usage.output_tokens"),
            "estimated_cost_usd": s.attributes.get("ai.cost.estimated_usd"),
            "error_type": s.attributes.get("ai.error.type"),
            "trace_id_present": s.context is not None and s.context.trace_id != 0,
        }
        for s in spans
        if s.name in ("llm.request", "embedding") and s.attributes
    ]
    key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else ""
    exported = "\n".join(s.to_json() for s in spans) + "\n".join(records)
    timeout = next((s for s in steps if s["step"] == "timeout"), {})
    checks = evidence_checks(steps, usage, bool(settings.model_pricing.prices))
    leaked = bool(key) and key in exported
    return {
        "smoke": "live-openai",
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "provider": "openai",
        "embedding_model": settings.embedding_model,
        "answer_model": settings.answer_model,
        "requests_budget": MAX_REQUESTS,
        # Reserved upper bound: a failed multi-call step may send fewer requests.
        "requests_reserved": requests,
        "steps": steps,
        "usage": usage,
        "pricing_configured": bool(settings.model_pricing.prices),
        "secret_in_logs_or_spans": leaked,
        "checks": checks,
        "passed": all(s["status"] in ("ok", "skipped_no_database") for s in steps)
        and timeout.get("timed_out") is True
        and all(checks.values())
        and not leaked,
        "scope": "integration evidence only; makes no retrieval/answer quality claim",
    }


def evidence_checks(
    steps: list[dict[str, Any]], usage: list[dict[str, Any]], pricing_configured: bool
) -> dict[str, bool]:
    """Observed data must satisfy the smoke claims; presence alone is insufficient."""
    successful = [item for item in usage if item.get("error_type") is None]
    answer = next((s for s in steps if s["step"] == "structured_generation"), {})
    rag = next((s for s in steps if s["step"] == "rag_end_to_end"), {})
    timeout = next((s for s in steps if s["step"] == "timeout"), {})
    return {
        "operations_observed": {"embedding", "answer", "plan"}
        <= {item.get("operation") for item in successful},
        "usage_captured": bool(successful)
        and all(
            type(item.get(field)) is int and item[field] >= 0
            for item in successful
            for field in ("input_tokens", "output_tokens")
        ),
        "served_model_recorded": bool(successful)
        and all(
            isinstance(item.get("served_model"), str) and item["served_model"]
            for item in successful
        ),
        "cost_captured_when_configured": not pricing_configured
        or (
            bool(successful)
            and all(item.get("estimated_cost_usd") is not None for item in successful)
        ),
        "trace_ids_present": bool(usage)
        and all(item.get("trace_id_present") is True for item in usage),
        "citations_within_evidence": answer.get("citations_within_evidence") is True,
        "rag_has_evidence": rag.get("status") == "skipped_no_database"
        or (rag.get("retrieved", 0) > 0 and rag.get("citations", 0) > 0),
        "timeout_classified_and_bounded": timeout.get("timed_out") is True
        and timeout.get("elapsed_ms", 1000) < 1000
        and any(item.get("error_type") == "timeout" for item in usage),
    }


def elapsed(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def settings_models(settings: Settings) -> list[str]:
    return sorted({settings.embedding_model, settings.answer_model})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    environ = dict(os.environ)
    refusal = gate_failure(environ)
    if refusal:
        print(refusal)
        sys.exit(2)  # fail closed: nothing external happened
    try:
        with observability.logs_to_stderr():
            result = asyncio.run(run(settings_from(environ)))
    except Exception as error:
        # Validation/transport exceptions may contain credentials or request content.
        result = {"smoke": "live-openai", "passed": False, "failure_type": type(error).__name__}
    encoded = json.dumps(result, indent=2, default=str)
    if environ["OPENAI_API_KEY"] in encoded:
        print("REFUSED: evidence would contain the key")
        sys.exit(1)
    if args.output:
        args.output.write_text(encoded + "\n")
    print(encoded)
    sys.exit(0 if result["passed"] and not result["secret_in_logs_or_spans"] else 1)


if __name__ == "__main__":
    main()
