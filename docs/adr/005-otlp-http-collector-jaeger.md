# ADR 005: OpenTelemetry SDK → OTLP/HTTP → Collector → Jaeger, in an optional profile

Status: accepted for Phase 3 (2026-10-04). Details: [observability.md](../observability.md).

## Context

Phase 3 needs traces and metrics that answer operational and AI questions without leaking content,
must not be able to take the product down, and must be viewable locally on a small VM without
making the normal stack heavier.

## Decision

- **In-process:** `opentelemetry-api` + `opentelemetry-sdk` with manual spans at business/AI
  boundaries, and the **OTLP/HTTP** exporter (`opentelemetry-exporter-otlp-proto-http`).
  Spans go through a `BatchSpanProcessor` and metrics through a `PeriodicExportingMetricReader`,
  both in background threads with 2 s timeouts, so export is never on the request path.
- **Out of process (Compose profile `observability`, optional):**
  - the OpenTelemetry Collector (contrib 0.161.0) receives OTLP/HTTP, applies `memory_limiter`,
    a defense-in-depth attribute redaction and batching;
  - traces go to **Jaeger 2.21.0** (in-memory);
  - metrics are exposed by the collector's **Prometheus exporter** on :8889.
- **Logs** stay JSON on stdout.

## Alternatives rejected

- **OTLP/gRPC exporter:** pulls in `grpcio`, a large native wheel, for no benefit at this volume.
  OTLP/HTTP uses the protobuf encoding and the standard 4318 endpoint.
- **Auto-instrumentation packages** (FastAPI, SQLAlchemy, httpx instrumentors): they add many
  dependencies and record URLs, statements and headers we would then have to scrub. The required
  spans are business boundaries that auto-instrumentation does not know about.
- **`prometheus_client` / a Prometheus exporter inside the API:** a second metrics stack and an
  extra scrape port on the API. With OTLP, a single SDK pipeline serves traces and metrics, and
  the Collector does the format conversion.
- **Prometheus server + Grafana + Tempo/Loki:** a full stack only for screenshots, too heavy for
  this VM (the Docker data root is a 4 GB tmpfs). Jaeger UI plus a curl-able metrics endpoint is
  enough to inspect both signals. A real deployment would point the same Collector at its backends.
- **Collector-less export straight to Jaeger:** possible for traces, but leaves metrics without a
  destination, and loses the central redaction/batching hop.
- **LangSmith/Datadog:** out of scope by owner decision. Vendor-neutral OTLP keeps that option
  open later.

## Consequences

- **Supply chain:** the Python side gains 10 packages (OTel API/SDK/exporter/proto/semconv,
  protobuf, googleapis-common-protos); no gRPC. The profile adds two images, pinned by tag.
- **Normal stack unchanged:** `db`, `migrate` and `api` still run without a collector, and with no
  endpoint configured nothing is exported.
- **Data loss window:** spans generated while the collector is down are dropped. This is
  accepted for a fail-open design and documented.
