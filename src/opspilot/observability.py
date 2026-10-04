"""OpenTelemetry traces and metrics plus JSON logs, with redaction and fail-open export.

Redaction model (see docs/observability.md):
- span attributes must be registered in SPAN_ATTRIBUTES with a type; unknown keys are
  dropped and string values must match SAFE_STRING, otherwise they become "redacted";
- exceptions are never recorded on spans (OTel would store the message); only the
  exception class name is kept as error.type;
- metric labels must be in METRIC_LABELS; unsafe or UUID-like values become "other";
- logs are fixed JSON fields only: never request text, documents, prompts or secrets.
Every telemetry call is fail-open: a telemetry failure never fails the request.
"""

import json
import logging
import re
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from opentelemetry import context as otel_context
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode

from opspilot.domain import Usage

request_id: ContextVar[str] = ContextVar("request_id", default="background")
run_id: ContextVar[str] = ContextVar("run_id", default="-")

SERVICE_NAME = "opspilot-api"
EXPORT_TIMEOUT_SECONDS = 2.0

logger = logging.getLogger("opspilot")
logger.setLevel(logging.INFO)
logger.propagate = True
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

tracer = trace.get_tracer("opspilot")
meter = metrics.get_meter("opspilot")


@contextmanager
def logs_to_stderr() -> Iterator[None]:
    """Temporarily route console logs away from a CLI's JSON stdout.

    Streams belong to the caller and may already be closed after a previous capture.
    Replacing handlers avoids setStream's flush of that borrowed stream, and restoring
    the original handlers keeps repeated CLI invocations from retaining stderr captures.
    """
    replaced: list[tuple[logging.Handler, logging.Handler]] = []
    for original in list(logger.handlers):
        if isinstance(original, logging.StreamHandler) and not isinstance(
            original, logging.FileHandler
        ):
            temporary = logging.StreamHandler(sys.stderr)
            temporary.setLevel(original.level)
            temporary.setFormatter(original.formatter)
            for log_filter in original.filters:
                temporary.addFilter(log_filter)
            logger.removeHandler(original)
            logger.addHandler(temporary)
            replaced.append((original, temporary))
    try:
        yield
    finally:
        for restored, replacement in replaced:
            logger.removeHandler(replacement)
            replacement.close()  # Handler.close does not close the caller's stream.
            logger.addHandler(restored)


# --------------------------------------------------------------------- redaction model
SAFE_STRING = re.compile(r"^[A-Za-z0-9_.:/{}-]{1,128}$")
SPAN_ATTRIBUTES: dict[str, type] = {
    "opspilot.request_id": str,
    "opspilot.run_id": str,
    "http.request.method": str,
    "http.route": str,
    "http.response.status_code": int,
    "error.type": str,
    "ai.provider": str,
    "ai.model": str,
    "ai.response.model": str,
    "ai.operation": str,
    "ai.error.type": str,
    "ai.usage.known": bool,
    "ai.usage.input_tokens": int,
    "ai.usage.output_tokens": int,
    "ai.usage.total_tokens": int,
    "ai.cost.known": bool,
    "ai.cost.estimated_usd": float,
    "ai.retrieval.strategy": str,
    "ai.retrieval.top_k": int,
    "ai.retrieval.candidates_requested": int,
    "ai.retrieval.candidates_returned": int,
    "ai.retrieval.chunks_returned": int,
    "ai.retrieval.inputs": int,
    "ai.context.chunks": int,
    "ai.context.characters": int,
    "ai.answer.abstained": bool,
    "ai.citations.count": int,
    "ai.ingest.chunks": int,
    "ai.agent.entry": str,
    "ai.agent.status": str,
    "ai.agent.steps": int,
    "ai.agent.step": int,
    "ai.tool.name": str,
    "ai.decision.result": str,
    "ai.policy.result": str,
    "ai.policy.reason": str,
    "ai.approval.decision": str,
    "ai.approval.wait_seconds": float,
    "ai.execution.mode": str,
    "ai.execution.outcome": str,
    "ai.reconciliation.result": str,
    "ai.gitlab.operation": str,
}
METRIC_LABELS = frozenset(
    {
        "provider",
        "model",
        "operation",
        "error_type",
        "outcome",
        "strategy",
        "tool",
        "reason",
        "status",
        "result",
        "mode",
        "entry",
        "route",
        "method",
        "status_class",
    }
)
SAFE_LABEL = re.compile(r"^[a-z0-9_.:/{}-]{1,64}$")
UUID_LIKE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def clean_attributes(attributes: Mapping[str, object]) -> dict[str, str | bool | int | float]:
    clean: dict[str, str | bool | int | float] = {}
    for key, value in attributes.items():
        kind = SPAN_ATTRIBUTES.get(key)
        if kind is None or value is None:
            continue
        if kind is bool and isinstance(value, bool):
            clean[key] = value
        elif kind is int and isinstance(value, int) and not isinstance(value, bool):
            clean[key] = value
        elif (
            kind is float
            and isinstance(value, int | float | Decimal)
            and not isinstance(value, bool)
        ):
            clean[key] = float(value)
        elif kind is str and isinstance(value, str):
            clean[key] = value if SAFE_STRING.fullmatch(value) else "redacted"
    return clean


def clean_labels(labels: Mapping[str, object]) -> dict[str, str]:
    clean: dict[str, str] = {}
    for key, value in labels.items():
        if key not in METRIC_LABELS:
            continue  # high-cardinality or unknown keys never become labels
        text = str(value).lower()
        clean[key] = text if SAFE_LABEL.fullmatch(text) and not UUID_LIKE.search(text) else "other"
    return clean


# --------------------------------------------------------------------- metric registry
COUNTERS = {
    name: meter.create_counter(name, unit=unit, description=description)
    for name, unit, description in (
        ("http_server_requests_total", "1", "HTTP requests by route template and status class"),
        ("llm_requests_total", "1", "Model/provider calls"),
        ("llm_failures_total", "1", "Failed model/provider calls by error type"),
        ("llm_timeouts_total", "1", "Model/provider calls that timed out"),
        ("llm_input_tokens_total", "{token}", "Input tokens reported by the provider"),
        ("llm_output_tokens_total", "{token}", "Output tokens reported by the provider"),
        ("llm_usage_unknown_total", "1", "Calls whose provider did not report usage"),
        ("llm_estimated_cost_usd_total", "USD", "Estimated cost where pricing is configured"),
        ("llm_cost_unknown_total", "1", "Calls whose cost is unknown (no price or no usage)"),
        ("retrieval_requests_total", "1", "Retrieval requests by strategy"),
        ("retrieval_empty_total", "1", "Retrievals returning no chunk"),
        ("agent_runs_total", "1", "Agent workflow invocations by entry point"),
        ("agent_success_total", "1", "Runs reaching answered or succeeded"),
        ("agent_failure_total", "1", "Runs reaching failed, by reason"),
        ("tool_calls_total", "1", "Tool executions by tool"),
        ("tool_failures_total", "1", "Failed tool executions by tool and error type"),
        ("approval_requested_total", "1", "Proposals stored and awaiting a human"),
        ("approval_approved_total", "1", "Approvals recorded"),
        ("approval_rejected_total", "1", "Rejections recorded"),
        ("policy_denied_total", "1", "Authorization denials by reason"),
        ("invalid_model_output_total", "1", "Model outputs rejected by schema/validation"),
        (
            "approval_hash_mismatch_total",
            "1",
            "Approvals or executions whose action hash did not match",
        ),
        ("unauthorized_tool_total", "1", "Model requests for tools it may not use"),
        ("ambiguous_execution_total", "1", "Side effects with unknown outcome"),
        ("reconciliation_attempt_total", "1", "Marker lookups for uncertain executions"),
        ("reconciliation_success_total", "1", "Reconciliations that found the issue"),
        ("telemetry_errors_total", "1", "Telemetry operations that failed (fail-open)"),
    )
}
HISTOGRAMS = {
    name: meter.create_histogram(name, unit=unit, description=description)
    for name, unit, description in (
        ("http_server_duration_seconds", "s", "HTTP request duration"),
        ("llm_duration_seconds", "s", "Model/provider call duration"),
        ("retrieval_duration_seconds", "s", "Retrieval duration by strategy"),
        ("retrieval_chunks_returned", "{chunk}", "Chunks returned per retrieval"),
        ("agent_steps", "{step}", "Steps used when a run stops"),
        ("agent_duration_seconds", "s", "Duration of one agent invocation"),
        ("tool_duration_seconds", "s", "Tool execution duration"),
        ("approval_duration_seconds", "s", "Human wait from proposal to decision"),
    )
}


def _telemetry_error() -> None:
    try:
        COUNTERS["telemetry_errors_total"].add(1)
    except Exception:  # noqa: BLE001 - telemetry must never raise into the product
        return


def count(name: str, value: float = 1, /, **labels: object) -> None:
    try:
        COUNTERS[name].add(value, clean_labels(labels))
    except Exception:  # noqa: BLE001 - fail-open
        _telemetry_error()


def observe(name: str, value: float, /, **labels: object) -> None:
    try:
        HISTOGRAMS[name].record(value, clean_labels(labels))
    except Exception:  # noqa: BLE001 - fail-open
        _telemetry_error()


def annotate(target: Span | None = None, **attributes: object) -> None:
    """Set allowlisted attributes; keys use '__' for '.', e.g. ai__tool__name -> ai.tool.name."""
    try:
        current = target or trace.get_current_span()
        current.set_attributes(
            clean_attributes({k.replace("__", "."): v for k, v in attributes.items()})
        )
    except Exception:  # noqa: BLE001 - fail-open
        _telemetry_error()


def trace_ids() -> tuple[str, str]:
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return "-", "-"
    return format(context.trace_id, "032x"), format(context.span_id, "016x")


def log_event(fields: Mapping[str, object], level: int = logging.INFO) -> None:
    """One JSON line on stdout. Callers pass fixed, non-content fields only."""
    try:
        trace_id, span_id = trace_ids()
        payload = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": logging.getLevelName(level),
            "request_id": request_id.get(),
            "run_id": run_id.get(),
            "trace_id": trace_id,
            "span_id": span_id,
            **fields,
        }
        logger.log(level, json.dumps(payload, default=str, separators=(",", ":")))
    except Exception:  # noqa: BLE001 - fail-open
        _telemetry_error()


@contextmanager
def span(
    name: str, parent: otel_context.Context | None = None, **attributes: object
) -> Iterator[Span]:
    """Business/AI boundary span. Attributes use '__' for '.', e.g. ai__tool__name."""
    started = time.monotonic()
    outcome = "ok"
    error_type: str | None = None
    with tracer.start_as_current_span(
        name, context=parent, record_exception=False, set_status_on_exception=False
    ) as current:
        correlation: dict[str, object] = {"opspilot.request_id": request_id.get()}
        if run_id.get() != "-":
            correlation["opspilot.run_id"] = run_id.get()
        annotate(current, **{k.replace(".", "__"): v for k, v in correlation.items()}, **attributes)
        try:
            yield current
        except BaseException as error:
            outcome, error_type = "error", type(error).__name__
            try:
                current.set_status(Status(StatusCode.ERROR))
                current.set_attribute("error.type", error_type)
            except Exception:  # noqa: BLE001 - fail-open
                _telemetry_error()
            raise
        finally:
            fields: dict[str, object] = {
                "operation": name,
                "outcome": outcome,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            }
            status = (getattr(current, "attributes", None) or {}).get("http.response.status_code")
            if isinstance(status, int):
                fields["status"] = status
            if error_type:
                fields["error_type"] = error_type
            log_event(fields)


# --------------------------------------------------------------------- LLM accounting
@dataclass
class UsageTotals:
    """Accumulates usage of every model call inside a scope (e.g. one planning step)."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    unknown_calls: int = 0
    cost_usd: Decimal = Decimal(0)
    unknown_cost_calls: int = 0

    def summary(self) -> dict[str, object]:
        return {
            "llm_calls": self.calls,
            "input_tokens": self.input_tokens if not self.unknown_calls else None,
            "output_tokens": self.output_tokens if not self.unknown_calls else None,
            "usage_unknown_calls": self.unknown_calls,
            "estimated_cost_usd": str(self.cost_usd) if not self.unknown_cost_calls else None,
        }


usage_scope_var: ContextVar[UsageTotals | None] = ContextVar("usage_scope", default=None)


@contextmanager
def usage_scope() -> Iterator[UsageTotals]:
    totals = UsageTotals()
    token = usage_scope_var.set(totals)
    try:
        yield totals
    finally:
        usage_scope_var.reset(token)


@dataclass
class LLMCall:
    provider: str
    model: str
    operation: str
    span: Span
    usage: Usage = field(default_factory=Usage)
    cost: Decimal | None = None
    error_type: str | None = None
    response_model: str | None = None

    def report_usage(
        self, usage: Usage, cost: Decimal | None, response_model: str | None = None
    ) -> None:
        """response_model: the identifier the provider says it served (span only, not a label)."""
        self.usage, self.cost, self.response_model = usage, cost, response_model

    def fail(self, error_type: str) -> None:
        self.error_type = error_type


ERROR_TYPES = frozenset(
    {
        "timeout",
        "rate_limit",
        "provider_4xx",
        "provider_5xx",
        "invalid_output",
        "refusal",
        "connection",
        "unknown",
    }
)


@contextmanager
def llm_call(provider: str, model: str, operation: str) -> Iterator[LLMCall]:
    """Span + metrics for one model/provider call; adapters report usage and error type."""
    name = "embedding" if operation == "embedding" else "llm.request"
    started = time.monotonic()
    with span(name, ai__provider=provider, ai__model=model, ai__operation=operation) as current:
        call = LLMCall(provider, model, operation, current)
        try:
            yield call
        except BaseException as error:
            if call.error_type is None:
                call.error_type = "timeout" if isinstance(error, TimeoutError) else "unknown"
            raise
        finally:
            _finish_llm_call(call, time.monotonic() - started)


def _finish_llm_call(call: LLMCall, seconds: float) -> None:
    labels = {"provider": call.provider, "model": call.model, "operation": call.operation}
    usage = call.usage
    count("llm_requests_total", **labels)
    outcome = "ok" if call.error_type is None else "error"
    observe("llm_duration_seconds", seconds, outcome=outcome, **labels)
    if call.error_type is not None:
        error = call.error_type if call.error_type in ERROR_TYPES else "unknown"
        count("llm_failures_total", error_type=error, **labels)
        if error == "timeout":
            count("llm_timeouts_total", **labels)
        annotate(call.span, ai__error__type=error)
    annotate(
        call.span,
        ai__usage__known=usage.known,
        ai__usage__input_tokens=usage.input_tokens,
        ai__usage__output_tokens=usage.output_tokens,
        ai__usage__total_tokens=usage.total_tokens,
        ai__cost__known=call.cost is not None,
        ai__cost__estimated_usd=call.cost,
        ai__response__model=call.response_model,
    )
    # Unknown usage is counted as unknown, never added as zero tokens.
    if usage.input_tokens is not None:
        count("llm_input_tokens_total", usage.input_tokens, **labels)
    if usage.output_tokens is not None:
        count("llm_output_tokens_total", usage.output_tokens, **labels)
    if not usage.known:
        count("llm_usage_unknown_total", **labels)
    if call.cost is not None:
        count("llm_estimated_cost_usd_total", float(call.cost), **labels)
    else:
        count("llm_cost_unknown_total", **labels)
    totals = usage_scope_var.get()
    if totals is not None:
        totals.calls += 1
        if usage.known:
            totals.input_tokens += usage.input_tokens or 0
            totals.output_tokens += usage.output_tokens or 0
        else:
            totals.unknown_calls += 1
        if call.cost is not None:
            totals.cost_usd += call.cost
        else:
            totals.unknown_cost_calls += 1


# --------------------------------------------------------------------- SDK setup
_providers: tuple[TracerProvider, MeterProvider] | None = None


def setup(endpoint: str | None, version: str = "0.1.0") -> bool:
    """Install SDK providers once. Without an endpoint, spans still carry trace IDs for
    log correlation but nothing is exported. Returns False if another SDK is installed.
    Export failures are absorbed by background processors (fail-open)."""
    global _providers
    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        resource = Resource.create({"service.name": SERVICE_NAME, "service.version": version})
        tracer_provider = TracerProvider(resource=resource)
        readers = []
        if endpoint:
            tracer_provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=f"{endpoint}/v1/traces", timeout=EXPORT_TIMEOUT_SECONDS
                    ),
                    max_queue_size=2048,
                    export_timeout_millis=EXPORT_TIMEOUT_SECONDS * 1000,
                )
            )
            readers.append(
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(
                        endpoint=f"{endpoint}/v1/metrics", timeout=EXPORT_TIMEOUT_SECONDS
                    ),
                    export_interval_millis=10_000,
                    export_timeout_millis=EXPORT_TIMEOUT_SECONDS * 1000,
                )
            )
        meter_provider = MeterProvider(resource=resource, metric_readers=readers)
        trace.set_tracer_provider(tracer_provider)
        metrics.set_meter_provider(meter_provider)
        _providers = (tracer_provider, meter_provider)
        _quiet_sdk_logs()
        return True
    except Exception:  # noqa: BLE001 - observability must not stop the API
        log_event({"operation": "telemetry.setup", "outcome": "error"}, logging.WARNING)
        return False


def shutdown() -> None:
    """Flush with bounded timeouts; never raises."""
    if _providers is None:
        return
    tracer_provider, meter_provider = _providers
    for close in (tracer_provider.shutdown, lambda: meter_provider.shutdown(timeout_millis=2000)):
        try:
            close()
        except Exception:  # noqa: BLE001 - fail-open
            _telemetry_error()


class _SdkLogFormatter(logging.Formatter):
    """SDK export errors as one JSON line, without exception text or stack traces."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "level": record.levelname,
                "operation": "telemetry.export",
                "logger": record.name,
                "outcome": "error",
            },
            separators=(",", ":"),
        )


def _quiet_sdk_logs() -> None:
    sdk_logger = logging.getLogger("opentelemetry")
    sdk_logger.handlers = []
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(_SdkLogFormatter())
    sdk_logger.addHandler(stream)
    sdk_logger.setLevel(logging.WARNING)
    sdk_logger.propagate = False
