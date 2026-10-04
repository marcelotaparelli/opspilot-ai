# Observability (Phase 3)

OpsPilot emits OpenTelemetry **traces** and **metrics**, writes **JSON logs** to stdout, and keeps
the Phase 2 **audit trail** in PostgreSQL. Instrumentation is manual and only at boundaries that
mean something for the business or the AI pipeline; trivial functions are not spanned. Telemetry
is **fail-open**, while authentication, policy and approval stay **fail-closed**. Export-path
decision: [ADR 005](adr/005-otlp-http-collector-jaeger.md).

## 1. Question → signal

| Question | Signal |
| --- | --- |
| Which request/run failed? | `http.request` span status + `x-request-id`/`x-trace-id` response headers; `agent_failure_total{reason}`; run `status`/`error` and audit events by `run_id` |
| Why was this request slow? | Trace breakdown: `http.request` → `rag.query` → `retrieval` → `embedding` / `vector_retrieval` / `lexical_retrieval` / `rank_fusion` → `context_build` → `llm.request` |
| Where does agent time go? | `agent.run` → `agent.plan` (`llm.request`) / `knowledge_search` / `authorization` / `approval` / `tool.execute` → `gitlab.request` / `reconciliation`; `agent_duration_seconds{entry}`, `tool_duration_seconds{tool}` |
| Which model/provider was called? | `llm.request` / `embedding` spans: `ai.provider`, `ai.model`, `ai.operation`; `llm_requests_total{provider,model,operation}` |
| How many tokens? | `ai.usage.*` span attributes; `llm_input_tokens_total`, `llm_output_tokens_total`; `llm_usage_unknown_total` when the provider did not say |
| What would it cost? | `ai.cost.estimated_usd`, `llm_estimated_cost_usd_total` (configured prices only), `llm_cost_unknown_total`; per-decision cost persisted in `llm.decision` audit events |
| Which retrieval strategy, how many chunks? | `retrieval` span: `ai.retrieval.strategy`, `top_k`, `candidates_requested`, `chunks_returned`; branch spans: `candidates_returned`; `retrieval_chunks_returned{strategy}`, `retrieval_empty_total{strategy}` |
| Which tool was proposed / executed? | `agent.plan`: `ai.tool.name`, `ai.decision.result`; `tool_calls_total{tool}`, `tool_failures_total{tool,error_type}` |
| Was there an approval, and how long did it take? | `approval` span: `ai.approval.decision`, `ai.approval.wait_seconds`; `approval_requested/approved/rejected_total`, `approval_duration_seconds` |
| Retry or reconciliation? | `tool.execute` `ai.execution.mode` (`create`/`reconcile`), `reconciliation` span result; `ambiguous_execution_total{reason}`, `reconciliation_attempt_total`, `reconciliation_success_total` |
| Did a policy block it? | `authorization` span `ai.policy.result`/`ai.policy.reason`; `policy_denied_total{reason}`, `unauthorized_tool_total`, `approval_hash_mismatch_total`; audit `policy.denied` / `tool.denied` |
| Are tools failing? | `tool_failures_total{tool,error_type}` |
| Are model calls failing, and how? | `llm_failures_total{error_type}` (timeout, rate_limit, provider_4xx, provider_5xx, invalid_output, refusal, connection, unknown), `llm_timeouts_total` |
| Did someone bypass approval? | Security regression suite S04/S05 in CI; `approval_hash_mismatch_total`; audit events `approval.*` / `approval.invalid` |
| Did quality regress after a change? | CI RAG gate on retrieval-v2 **dev**; agent gate on agent-v1 (§8) |
| Is telemetry itself healthy? | `telemetry_errors_total`; `telemetry.export` JSON log lines |

## 2. Export path

```
API process (OpenTelemetry SDK)
  spans   → BatchSpanProcessor (queue 2048, 2 s export timeout)          ─┐
  metrics → PeriodicExportingMetricReader (10 s interval, 2 s timeout)   ─┤ OTLP/HTTP :4318
  logs    → JSON lines on stdout (not exported via OTLP)                  │
                                                                          ▼
OpenTelemetry Collector (contrib 0.161.0, profile "observability")
  memory_limiter → attributes/redact (defense in depth) → batch
  traces  → Jaeger 2.21.0 (in-memory; UI http://127.0.0.1:16686)
  metrics → Prometheus text endpoint http://127.0.0.1:8889/metrics
```

When `OTEL_EXPORTER_OTLP_ENDPOINT` is unset (the normal stack), spans are still created, so logs
and audit events carry trace IDs, but nothing is exported.

## 3. Traces

| Span | Parent | Key attributes |
| --- | --- | --- |
| `http.request` | incoming W3C `traceparent`, if any | `http.request.method`, `http.route` (template), `http.response.status_code`, `opspilot.request_id` |
| `rag.ingest` | `http.request` | `ai.ingest.chunks` |
| `rag.query` | `http.request` | `ai.retrieval.top_k`, `ai.retrieval.chunks_returned`, `ai.citations.count`, `ai.answer.abstained` |
| `retrieval` | `rag.query` / `knowledge_search` | `ai.retrieval.strategy`, `.top_k`, `.candidates_requested`, `.chunks_returned` |
| `embedding` | `retrieval` / `rag.ingest` | `ai.provider`, `ai.model`, `ai.operation=embedding`, usage/cost |
| `vector_retrieval`, `lexical_retrieval` | `retrieval` | `ai.retrieval.candidates_returned` |
| `rank_fusion` | `retrieval` | `ai.retrieval.inputs`, `.chunks_returned` |
| `context_build` | `rag.query` | `ai.context.chunks`, `ai.context.characters` |
| `llm.request` | `rag.query` / `agent.plan` | `ai.provider`, `ai.model`, `ai.operation` (`answer`/`plan`), `ai.usage.*`, `ai.cost.*`, `ai.error.type` |
| `agent.run` | `http.request` | `opspilot.run_id`, `ai.agent.entry` (start/approve/reject/resume), `ai.agent.status`, `ai.agent.steps` |
| `agent.plan` | `agent.run` | `ai.agent.step`, `ai.tool.name`, `ai.decision.result` |
| `knowledge_search` | `agent.run` | `ai.tool.name` |
| `authorization` | `agent.run` | `ai.operation` (start/tool/prepare/decide/execute), `ai.policy.result`, `ai.policy.reason` |
| `approval` | `agent.run` | `ai.approval.decision`, `ai.approval.wait_seconds` |
| `tool.execute` | `agent.run` | `ai.tool.name=create_gitlab_issue`, `ai.execution.mode`, `ai.execution.outcome` |
| `reconciliation` | `tool.execute` | `ai.reconciliation.result` (found/not_found/within_grace/unavailable) |
| `gitlab.request` | `tool.execute` / `reconciliation` | `ai.gitlab.operation` (create/lookup), `http.request.method`, `http.response.status_code` |

Every span inside a run carries `opspilot.run_id`; every span carries `opspilot.request_id`.
Failures set span status ERROR and `error.type` to the exception **class name** only.

Semantic attributes are project-specific (`ai.*`, `opspilot.*`) where no stable OpenTelemetry
convention exists; HTTP attributes follow the standard names. The registry is
`SPAN_ATTRIBUTES` in `src/opspilot/observability.py`. A key not listed there is dropped.

## 4. Metrics

All metrics live in one registry (`COUNTERS`/`HISTOGRAMS`); names are exported unchanged
(`add_metric_suffixes: false`).

| Area | Metric | Labels |
| --- | --- | --- |
| HTTP | `http_server_requests_total`, `http_server_duration_seconds` | `method`, `route` (template), `status_class` |
| LLM | `llm_requests_total`, `llm_failures_total`, `llm_timeouts_total`, `llm_duration_seconds`, `llm_input_tokens_total`, `llm_output_tokens_total`, `llm_usage_unknown_total`, `llm_estimated_cost_usd_total`, `llm_cost_unknown_total` | `provider`, `model`, `operation`, (`error_type`, `outcome`) |
| Retrieval | `retrieval_requests_total`, `retrieval_duration_seconds`, `retrieval_chunks_returned`, `retrieval_empty_total` | `strategy` (lexical/vector/hybrid) |
| Agent | `agent_runs_total`, `agent_success_total`, `agent_failure_total`, `agent_steps`, `agent_duration_seconds` | `entry`, `status`, `reason` |
| Tools | `tool_calls_total`, `tool_failures_total`, `tool_duration_seconds` | `tool`, `error_type` |
| Approval | `approval_requested_total`, `approval_approved_total`, `approval_rejected_total`, `approval_duration_seconds` | `result` |
| Security | `policy_denied_total`, `invalid_model_output_total`, `approval_hash_mismatch_total`, `unauthorized_tool_total` | `reason` |
| Reconciliation | `ambiguous_execution_total`, `reconciliation_attempt_total`, `reconciliation_success_total` | `reason` |
| Telemetry | `telemetry_errors_total` | none |

Durations use `time.monotonic()`. The exception is `approval_duration_seconds`: a human wait that
spans processes, so it is measured from PostgreSQL transaction timestamps (proposal `created_at` →
approval `decided_at`).

## 5. Logs

One JSON object per line on stdout:
`ts, level, request_id, run_id, trace_id, span_id, operation, outcome, duration_ms[, error_type]`.

Logs never contain request text, documents, prompts, answers, tokens, connection strings or
exception messages. OpenTelemetry SDK export failures appear as
`{"operation":"telemetry.export","outcome":"error",...}` without stack traces or URLs. httpx,
httpcore, openai, sqlalchemy and uvicorn.error loggers stay silenced, as in Phase 1.

## 6. Correlation

- `request_id` is server-generated, returned in `x-request-id` and present in every log line and span.
- `trace_id` is returned in `x-trace-id`. An incoming W3C `traceparent` is continued (a client may
  choose the trace ID, which is harmless; `request_id` stays server-generated). Outgoing GitLab
  requests carry `traceparent`; OpenAI requests do not (no trace context sent to the model provider).
- `run_id` is on every agent span and log line. Every audit event stores the `trace_id` that
  produced it, so an operator can go audit → trace → logs.

Verified by `test_agent_run_is_reconstructable_from_traces_audit_and_logs`. A single run (start →
approve → ambiguous → resume) is reconstructed from spans: the three `agent.run` entries, plan,
search, all policy checks, approval, both `tool.execute` outcomes and the reconciliation. Its audit
`trace_id`s and log lines map to the same traces, without reading any document or prompt.

## 7. Redaction, cardinality and leak testing

- **Span attributes:** only keys in the registry, with the declared type. String values must match
  `[A-Za-z0-9_.:/{}-]{1,128}`, otherwise they become `redacted`. Exceptions are not recorded
  (`record_exception=False`), because OpenTelemetry would otherwise store the exception message
  as an event.
- **Metric labels:** only `provider, model, operation, error_type, outcome, strategy, tool,
  reason, status, result, mode, entry, route, method, status_class`. Other keys are dropped, and
  unsafe or UUID-shaped values become `other`. Never labels: `request_id`, `run_id`, `tenant_id`,
  document/chunk/issue IDs, subjects, error messages, raw URLs (routes are templates).
- **Cardinality audit:** `test_metric_labels_stay_bounded_after_real_flows` runs mixed flows for
  two tenants and checks every exported data point. Across the whole suite, no metric has more than
  40 label combinations.
- **Collector:** `attributes/redact` deletes any attribute whose key looks like a credential or
  prompt, as defense in depth.
- **Sentinel leak tests:** `TEST_SECRET_DO_NOT_LOG_*` values go through the bearer token (valid and
  invalid), the GitLab token, a document, a RAG question, an agent prompt and an internal exception
  message. Test `test_sentinel_secrets_never_reach_logs_traces_or_metrics` and security case S09
  capture every log record (root at DEBUG), stdout/stderr, every finished span (attributes, events,
  status, resource) and every metric label: zero occurrences.
- A live check of Jaeger's stored spans showed only allowlisted attribute keys.

## 8. Token and cost accounting

`Usage(input_tokens, output_tokens, total_tokens)`, where **`None` means unknown, never 0**:
- OpenAI Responses: `usage.input_tokens/output_tokens/total_tokens`.
- OpenAI embeddings: `prompt_tokens` as input; output is 0 by definition, since nothing is generated.
- Fake providers: unknown.

An unknown call increments `llm_usage_unknown_total` and adds nothing to the token counters.

**Pricing** is the only place a price exists (`src/opspilot/pricing.py`). It is configured through
`MODEL_PRICING`, a JSON list of
`{provider, model, input_cost_per_million, output_cost_per_million, effective_from}` with a
timezone required. The newest entry whose `effective_from ≤ call time` applies. A missing price or
unknown usage gives cost **unknown**, never zero and never invented. No prices are shipped by
default.

Costs are computed at call time and persisted in the `llm.decision` audit event (`input_tokens`,
`output_tokens`, `estimated_cost_usd`). Adding a newer price therefore never rewrites history;
`test_cost_is_persisted_at_call_time_and_not_rewritten` checks this. RAG answer costs are metrics
only (not persisted).

## 9. Failure behaviour

| Failure | Behaviour | Evidence |
| --- | --- | --- |
| Collector down | Export runs in background threads with 2 s timeouts; spans from that window are dropped; requests unaffected | `test_dead_collector_never_breaks_product_and_export_recovers`: RAG 200, agent issue `succeeded`, unauthenticated 401; p95 52 ms / max 85 ms with a dead endpoint; traces and metrics received again once a receiver appears; shutdown 0.002 s |
| Collector stopped in the live stack | Both smokes pass, 401 still enforced, API healthy, 4 `telemetry.export` error lines; Jaeger unchanged during the outage, then receives new spans after restart (80 → 95) | [record](evidence/phase3/observability-stack.txt) |
| Meter/tracer raises | Swallowed, `telemetry_errors_total` +1, request served; auth still 401 | `test_broken_telemetry_never_breaks_requests` |
| SDK setup error | Logged once as JSON, API starts without telemetry | `observability.setup` |

Telemetry never decides anything: authentication, policy and approval do not read telemetry state.

## 10. Regression gates (CI)

- **RAG**: `opspilot.benchmark run --split dev`, then `scripts.regression_gate rag`.
  - The baseline [`evals/regression/rag-dev-baseline.json`](../evals/regression/rag-dev-baseline.json)
    is the retrieval-v2 dev result. The consumed held-out set is never re-run; a test asserts
    `--split heldout` is absent from CI.
  - Tolerance is **0.042**, one query's worth on 24 queries (1/24). The fixed-layout fake-embedder
    run is deterministic, so any change is real, but only a drop larger than one query is gated.
  - The gate also fails if `k` or the embedding space changes, if hybrid stops being RRF of both
    branches, or if the lexical branch is empty more often than in the baseline.
- **Agent**: `scripts.agent_eval` followed by `scripts.regression_gate agent`, with policy file
  [`agent-gate.json`](../evals/regression/agent-gate.json).
  - Unauthorized actions, approval bypasses and duplicate side effects must be exactly 0;
    they are invariants.
  - Task success and terminal-state correctness must be 1.0, because all 16 cases are
    deterministic: a failure is a behaviour change, not noise.
- **Security**: `scripts.security_suite` ([cases](../evals/security-v1/cases.json), 10 cases)
  followed by `scripts.regression_gate security`. Any failed or missing case fails CI.

## 11. Local visualisation

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318 \
  docker compose -f compose.yaml -f compose.smoke.yaml --env-file .env.example \
  --profile observability up --build -d --wait
# Traces: http://127.0.0.1:16686 (UI) or the API:
curl "http://127.0.0.1:16686/api/v3/traces?query.service_name=opspilot-api&query.start_time_min=<RFC3339>&query.start_time_max=<RFC3339>"
# Metrics (Prometheus text):
curl http://127.0.0.1:8889/metrics | grep -E '^(llm_|retrieval_|agent_|tool_|approval_|policy_)'
```

Measured in this VM ([record](evidence/phase3/observability-stack.txt)):
- After both smokes, Jaeger held 80 spans with 17 distinct names. Every span in §3 appeared except
  `reconciliation`, which the smoke does not trigger (the integration tests do).
- No stored attribute key was outside the allowlist.
- The collector's metrics carried no `run_id`, `tenant_id`, `request_id`, `document_id`,
  `chunk_id` or `subject` label.
The collector endpoint served `llm_requests_total`, `http_server_requests_total{route="/v1/agent/runs/{run_id}/approve",...}`
and the agent/approval counters, with no IDs as labels.

## 12. Performance baseline

Environment: a VM with 4 vCPU (AMD EPYC), 7.8 GiB RAM, Linux 6.12. Docker data root is on tmpfs.
A single uvicorn worker serves requests. PostgreSQL 17.6 and the API run in Compose on the same
VM, and the load generator (`scripts/load_test.py`, httpx) also runs on that VM. The fake
providers are used, with 8 concurrent clients, warmup excluded, and telemetry exported to the
collector unless noted. **Numbers are a baseline for this machine only.**

| Path | Requests | p50 ms | p95 ms | p99 ms | Throughput | Errors |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| RAG `POST /v1/query`, run 1 | 400 | 38.4 | 73.8 | 244.2 | 164.6 rps | 0 |
| RAG run 2 | 400 | 39.5 | 101.4 | 141.4 | 159.2 rps | 0 |
| RAG without OTLP export | 400 | 37.7 | 104.8 | 183.2 | 167.4 rps | 0 |
| Agent read-only `POST /v1/agent/runs` (answered, 1 step), run 1 | 300 | 164.7 | 254.1 | 370.8 | 44.4 rps | 0 |
| Agent read-only, run 2 | 300 | 178.1 | 254.6 | 308.3 | 42.5 rps | 0 |

Observations:
- **CPU:** the API container peaks at about 87–127% CPU (one core saturated by the single worker)
  and PostgreSQL at 22–35%.
- **Agent latency:** an agent run makes several persisted transitions (run, planning state, audit
  events, reloads), which accounts for its higher latency.
- **Export overhead:** within run-to-run noise.
- **Tail latency:** p99 varies between runs (141–244 ms on RAG), so treat it as indicative.
- **Memory soak:** API memory over 5,000 additional requests went 104.9 → 105.4 → 105.7 → 105.3 MiB
  (RAG ×2000, agent ×1000, RAG ×2000), so there is no evident leak
  ([record](evidence/phase3/soak-memory.txt)).

## 13. Known limitations

- **Logs are not exported over OTLP.** Ship stdout with the platform's log agent.
- **Spans produced while the collector is down are dropped** after a failed export (bounded queue).
  Metrics are cumulative, so counts recover at the next successful export.
- **Local visualisation only.** Jaeger stores traces in memory. The metrics endpoint is a
  Prometheus *exposition* endpoint, with no Prometheus server, dashboards or alert rules.
- **No sampling configuration.** Every span is recorded (parent-based always-on). Configure sampling
  before high-volume use.
- **Unverified pricing and usage:** no real provider prices are shipped or verified, and fake
  providers report unknown usage. OpenAI usage parsing is tested only against mocked responses.
- **Uninstrumented internals:** LangGraph's own scheduling is not traced, only our node boundaries.
  Database statements are not traced; persistence time shows as the gap inside the parent span.
- **Bounded detail on denials:** the `authorization` span for a denied request ends in ERROR with
  `error.type=Denied`, and the reason is in `ai.policy.reason`.
- **Narrow benchmark:** one VM, one worker, fake providers, and the client on the same host. Not
  capacity planning.
