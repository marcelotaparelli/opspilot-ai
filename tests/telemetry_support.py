"""In-memory OpenTelemetry capture for tests (one global SDK per process)."""

from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

Point = tuple[str, frozenset[tuple[str, str]]]


@dataclass
class Telemetry:
    spans_exporter: InMemorySpanExporter
    reader: InMemoryMetricReader

    def spans(self) -> list[ReadableSpan]:
        return list(self.spans_exporter.get_finished_spans())

    def clear(self) -> None:
        self.spans_exporter.clear()

    def metrics(self) -> dict[Point, float]:
        """Cumulative value per (metric, labels): sums for counters, counts for histograms."""
        values: dict[Point, float] = {}
        data = self.reader.get_metrics_data()
        for resource in data.resource_metrics if data else []:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    for point in metric.data.data_points:
                        attrs = point.attributes or {}
                        key = (metric.name, frozenset((k, str(v)) for k, v in attrs.items()))
                        values[key] = float(getattr(point, "value", getattr(point, "count", 0)))
        return values

    def total(self, snapshot: Mapping[Point, float], name: str, **labels: str) -> float:
        wanted = set(labels.items())
        return sum(v for (n, attrs), v in snapshot.items() if n == name and wanted <= set(attrs))

    def delta(self, before: Mapping[Point, float], name: str, **labels: str) -> float:
        return self.total(self.metrics(), name, **labels) - self.total(before, name, **labels)


_installed: Telemetry | None = None


def install() -> Telemetry:
    global _installed
    if _installed is None:
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        reader = InMemoryMetricReader()
        trace.set_tracer_provider(provider)
        metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
        _installed = Telemetry(exporter, reader)
    return _installed


def by_name(spans: list[ReadableSpan]) -> Counter[str]:
    return Counter(span.name for span in spans)


def every_text(spans: list[ReadableSpan]) -> Iterator[str]:
    """Every string a span exports: names, attributes, events, status, resource."""
    for span in spans:
        yield span.name
        yield str(span.status.description or "")
        for value in (span.attributes or {}).values():
            yield str(value)
        for event in span.events:
            yield event.name
            yield str(dict(event.attributes or {}))
        yield str(dict(span.resource.attributes))
