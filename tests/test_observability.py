import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr

from opspilot import observability
from opspilot.api.app import create_app
from opspilot.application import RagService
from opspilot.config import Settings
from opspilot.domain import ProviderError, Usage
from opspilot.observability import (
    METRIC_LABELS,
    clean_attributes,
    clean_labels,
    count,
    llm_call,
    span,
    usage_scope,
)
from opspilot.pricing import ModelPricing, PricingTable
from opspilot.providers.openai import OpenAIProvider
from tests.helpers import TENANT_A, TOKEN_A, MemoryRepository, RecordingProvider
from tests.telemetry_support import Telemetry

JAN, JUN = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC)
PRICES = PricingTable(
    prices=(
        ModelPricing(
            provider="openai",
            model="gpt-4.1-mini",
            input_cost_per_million=Decimal("0.40"),
            output_cost_per_million=Decimal("1.60"),
            effective_from=JAN,
        ),
        ModelPricing(
            provider="openai",
            model="gpt-4.1-mini",
            input_cost_per_million=Decimal("0.20"),
            output_cost_per_million=Decimal("0.80"),
            effective_from=JUN,
        ),
    )
)


# ------------------------------------------------------------------ redaction / labels


def test_span_attributes_are_allowlisted_typed_and_sanitised() -> None:
    cleaned = clean_attributes(
        {
            "ai.tool.name": "search_knowledge",
            "ai.retrieval.top_k": 5,
            "ai.usage.known": True,
            "ai.cost.estimated_usd": Decimal("0.0001"),
            "prompt": "full prompt text",
            "http.request.header.authorization": "Bearer x",
            "db.statement": "SELECT secret",
            "ai.model": "has spaces and\nnewlines",
            "ai.retrieval.top_k_bad": 5,
            "ai.agent.steps": "3",
            "ai.usage.input_tokens": True,
        }
    )
    assert cleaned == {
        "ai.tool.name": "search_knowledge",
        "ai.retrieval.top_k": 5,
        "ai.usage.known": True,
        "ai.cost.estimated_usd": 0.0001,
        "ai.model": "redacted",
    }


def test_metric_labels_reject_high_cardinality_keys_and_values() -> None:
    labels = clean_labels(
        {
            "tool": "search_knowledge",
            "run_id": "3f2b0c1e-1111-4111-8111-111111111111",
            "tenant_id": "x",
            "request_id": "y",
            "reason": "c0ffee00-0000-4000-8000-000000000000",
            "error_type": "Some Free Text: with spaces",
            "model": "GPT-4.1-mini",
        }
    )
    assert labels == {
        "tool": "search_knowledge",
        "reason": "other",
        "error_type": "other",
        "model": "gpt-4.1-mini",
    }
    forbidden = {
        "request_id",
        "run_id",
        "tenant_id",
        "document_id",
        "chunk_id",
        "issue_id",
        "subject",
    }
    assert not forbidden & METRIC_LABELS


def test_exception_messages_never_reach_spans(telemetry: Telemetry) -> None:
    with pytest.raises(RuntimeError), span("rag.query"):
        raise RuntimeError("TEST_SECRET_DO_NOT_LOG_EXCEPTION connection string")
    (finished,) = telemetry.spans()
    assert finished.attributes is not None and finished.attributes["error.type"] == "RuntimeError"
    assert not finished.events and not finished.status.description
    assert "TEST_SECRET" not in str(finished.to_json())


# ------------------------------------------------------------------ tokens and cost


def test_unknown_usage_is_never_counted_as_zero(telemetry: Telemetry) -> None:
    before = telemetry.metrics()
    with usage_scope() as totals, llm_call("fake", "fake-model", "plan"):
        pass
    labels = {"provider": "fake", "model": "fake-model", "operation": "plan"}
    assert telemetry.delta(before, "llm_requests_total", **labels) == 1
    assert telemetry.delta(before, "llm_usage_unknown_total", **labels) == 1
    assert telemetry.delta(before, "llm_cost_unknown_total", **labels) == 1
    assert telemetry.delta(before, "llm_input_tokens_total", **labels) == 0
    summary = totals.summary()
    assert summary["input_tokens"] is None and summary["estimated_cost_usd"] is None
    (finished,) = telemetry.spans()
    assert finished.attributes is not None
    assert finished.attributes["ai.usage.known"] is False
    assert "ai.usage.input_tokens" not in finished.attributes


def test_known_usage_and_cost_are_recorded(telemetry: Telemetry) -> None:
    before = telemetry.metrics()
    usage = Usage(1000, 500, 1500)
    cost = PRICES.cost("openai", "gpt-4.1-mini", usage, datetime(2026, 3, 1, tzinfo=UTC))
    assert cost == Decimal("0.0012")  # 1000*0.40/1e6 + 500*1.60/1e6
    with usage_scope() as totals, llm_call("openai", "gpt-4.1-mini", "answer") as call:
        call.report_usage(usage, cost)
    labels = {"provider": "openai", "model": "gpt-4.1-mini", "operation": "answer"}
    assert telemetry.delta(before, "llm_input_tokens_total", **labels) == 1000
    assert telemetry.delta(before, "llm_output_tokens_total", **labels) == 500
    assert telemetry.delta(before, "llm_estimated_cost_usd_total", **labels) == pytest.approx(
        0.0012
    )
    assert telemetry.delta(before, "llm_usage_unknown_total", **labels) == 0
    assert totals.summary()["estimated_cost_usd"] == "0.0012"


def test_pricing_is_effective_dated_and_unknown_when_unconfigured() -> None:
    usage = Usage(1_000_000, 1_000_000, 2_000_000)
    march, july = datetime(2026, 3, 1, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC)
    assert PRICES.cost("openai", "gpt-4.1-mini", usage, march) == Decimal("2.00")
    assert PRICES.cost("openai", "gpt-4.1-mini", usage, july) == Decimal("1.00")
    assert PRICES.cost("openai", "gpt-4.1-mini", usage, JAN - timedelta(days=1)) is None
    assert PRICES.cost("openai", "other-model", usage, july) is None
    assert PRICES.cost("openai", "gpt-4.1-mini", Usage(10, None, None), july) is None
    # Adding a newer price never changes what an older call cost.
    newer = PricingTable(
        prices=(
            *PRICES.prices,
            PRICES.prices[0].model_copy(
                update={
                    "effective_from": datetime(2026, 9, 1, tzinfo=UTC),
                    "input_cost_per_million": Decimal("9"),
                }
            ),
        )
    )
    assert newer.cost("openai", "gpt-4.1-mini", usage, march) == Decimal("2.00")
    with pytest.raises(ValueError):
        ModelPricing(
            provider="openai",
            model="m",
            input_cost_per_million=Decimal(1),
            output_cost_per_million=Decimal(1),
            effective_from=datetime(2026, 1, 1),  # naive: ambiguous, rejected
        )


# ------------------------------------------------------------------ provider error taxonomy


def openai_settings(**extra: Any) -> Settings:
    return Settings.model_validate(
        {
            "database_url": SecretStr("postgresql+asyncpg://u:p@h/d"),
            "tenant_tokens": {TOKEN_A: str(TENANT_A)},
            "provider": "openai",
            "openai_api_key": SecretStr("mock-key"),
            "provider_timeout_seconds": 1.0,
            "model_pricing": PRICES,
        }
        | extra
    )


def provider(handler: Any) -> OpenAIProvider:
    client = AsyncOpenAI(
        api_key="mock-key",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return OpenAIProvider(openai_settings(), client)


def responses_body(content: dict[str, Any], usage: dict[str, int] | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "resp",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": "gpt-4.1-mini",
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "output": [
            {
                "type": "message",
                "id": "m",
                "role": "assistant",
                "status": "completed",
                "content": [content],
            }
        ],
    }
    if usage is not None:
        body["usage"] = usage | {
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        }
    return body


ANSWER: dict[str, Any] = {
    "type": "output_text",
    "text": json.dumps({"answer": "ok", "cited_chunk_ids": []}),
    "annotations": [],
}


@pytest.mark.parametrize(
    ("scenario", "error_type"),
    [
        ("timeout", "timeout"),
        ("429", "rate_limit"),
        ("400", "provider_4xx"),
        ("503", "provider_5xx"),
        ("invalid", "invalid_output"),
        ("refusal", "refusal"),
    ],
)
async def test_provider_errors_are_classified(
    telemetry: Telemetry, scenario: str, error_type: str
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if scenario == "timeout":
            import asyncio

            await asyncio.sleep(3)
        if scenario.isdigit():
            return httpx.Response(
                int(scenario), json={"error": {"message": "TEST_SECRET_DO_NOT_LOG_PROVIDER"}}
            )
        content: dict[str, Any] = (
            {"type": "refusal", "refusal": "no"}
            if scenario == "refusal"
            else {"type": "output_text", "text": "{not json", "annotations": []}
        )
        return httpx.Response(200, json=responses_body(content, None))

    before = telemetry.metrics()
    adapter = provider(handler)
    from opspilot.chunking import split_document
    from opspilot.domain import Document, Metadata

    chunk = split_document(Document(TENANT_A, TENANT_A, "evidence", Metadata("t")))[0]
    try:
        with pytest.raises(ProviderError):
            await adapter.answer("q", (chunk,))
    finally:
        await adapter.close()
    labels = {"provider": "openai", "operation": "answer"}
    assert telemetry.delta(before, "llm_failures_total", error_type=error_type, **labels) == 1
    assert telemetry.delta(before, "llm_timeouts_total", **labels) == (scenario == "timeout")
    texts = " ".join(str(s.to_json()) for s in telemetry.spans())
    assert "TEST_SECRET" not in texts


@pytest.mark.parametrize("with_usage", [True, False])
async def test_provider_usage_is_normalised_and_priced(
    telemetry: Telemetry, with_usage: bool
) -> None:
    usage = (
        {"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500} if with_usage else None
    )
    adapter = provider(lambda request: httpx.Response(200, json=responses_body(ANSWER, usage)))
    from opspilot.chunking import split_document
    from opspilot.domain import Document, Metadata

    chunk = split_document(Document(TENANT_A, TENANT_A, "evidence", Metadata("t")))[0]
    before = telemetry.metrics()
    try:
        await adapter.answer("q", (chunk,))
    finally:
        await adapter.close()
    labels = {"provider": "openai", "model": "gpt-4.1-mini", "operation": "answer"}
    if with_usage:
        assert telemetry.delta(before, "llm_input_tokens_total", **labels) == 1200
        assert telemetry.delta(before, "llm_output_tokens_total", **labels) == 300
        # Priced at "now" with the newest effective entry (0.20 / 0.80 per million).
        assert telemetry.delta(before, "llm_estimated_cost_usd_total", **labels) == pytest.approx(
            (1200 * 0.20 + 300 * 0.80) / 1_000_000
        )
    else:
        assert telemetry.delta(before, "llm_usage_unknown_total", **labels) == 1
        assert telemetry.delta(before, "llm_cost_unknown_total", **labels) == 1
        assert telemetry.delta(before, "llm_input_tokens_total", **labels) == 0


# ------------------------------------------------------------------ fail-open


async def test_broken_telemetry_never_breaks_requests(
    telemetry: Telemetry, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken:
        def add(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("meter exploded")

        def record(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("meter exploded")

        def set_attributes(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("tracer exploded")

    for name in list(observability.COUNTERS):
        if name != "telemetry_errors_total":
            monkeypatch.setitem(observability.COUNTERS, name, Broken())
    for name in list(observability.HISTOGRAMS):
        monkeypatch.setitem(observability.HISTOGRAMS, name, Broken())
    before = telemetry.metrics()
    repository, recording = MemoryRepository(), RecordingProvider()
    app = create_app(settings, RagService(repository, recording, recording))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as http:
        auth = {"Authorization": f"Bearer {TOKEN_A}"}
        created = await http.post(
            "/v1/documents", headers=auth, json={"content": "restart", "metadata": {"title": "t"}}
        )
        answered = await http.post("/v1/query", headers=auth, json={"question": "restart"})
        denied = await http.post("/v1/query", json={"question": "restart"})
    assert (created.status_code, answered.status_code) == (201, 200)
    assert denied.status_code == 401  # security stays fail-closed
    assert telemetry.delta(before, "telemetry_errors_total") > 0


def test_count_and_annotate_swallow_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(observability.COUNTERS, "tool_calls_total", object())
    count("tool_calls_total", tool="x")  # must not raise
    observability.annotate(object(), ai__tool__name="x")  # type: ignore[arg-type]


def test_logs_are_single_line_json_with_correlation(caplog: pytest.LogCaptureFixture) -> None:
    token = observability.run_id.set("11111111-1111-4111-8111-111111111111")
    try:
        with caplog.at_level(logging.INFO, logger="opspilot"), span("agent.plan"):
            pass
    finally:
        observability.run_id.reset(token)
    (line,) = [r.getMessage() for r in caplog.records if "agent.plan" in r.getMessage()]
    record = json.loads(line)
    assert record["operation"] == "agent.plan" and record["outcome"] == "ok"
    assert record["run_id"] == "11111111-1111-4111-8111-111111111111"
    assert len(record["trace_id"]) == 32 and record["duration_ms"] >= 0
    assert set(record) == {
        "ts",
        "level",
        "request_id",
        "run_id",
        "trace_id",
        "span_id",
        "operation",
        "outcome",
        "duration_ms",
    }
