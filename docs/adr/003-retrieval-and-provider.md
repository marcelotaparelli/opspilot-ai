# ADR 003: Exact vectors, simple full-text search and deterministic RRF

Status: accepted; retrieval-v2 measured lexical, fake-vector and hybrid on dev and the
once-consumed held-out split. Phase 3 measured local fake-provider load latency. Real semantic
embedding/answer quality and AWS performance remain unmeasured. Current gates and provenance
are in the [validation record](../VALIDATION.md).

Use exact cosine retrieval on 256-dimensional pgvector values and GIN-indexed PostgreSQL
`simple` full-text search, then RRF with constant 60. Fixed UUID tie breaks make a fixed
corpus repeatable. This avoids score calibration across fundamentally different ranking
scales. Four times K candidates per branch is a bounded initial heuristic, not a tuned value.

Exact vectors avoid approximate-search recall interactions with tenant filtering and make
isolation easier to reason about. They cost linear distance computation per tenant corpus;
no performance claim or ANN tuning is made. The simple dictionary is language-independent
but has no stemming and no stopword list. Because of that, AND semantics
(`websearch_to_tsquery`) made natural-language questions match nothing; validation replaced
it with OR over the question's word tokens. Under OR, `ts_rank_cd` cover density degenerated
to occurrence counting and favoured long, stopword-dense chunks, so ranking moved to `ts_rank`,
decided on the retrieval-v2 dev split (docs/evaluation/retrieval-v2.md). There is still no IDF. RRF's 60 is the constant from Cormack, Clarke &
Buettcher (SIGIR 2009), kept as the `rrf()` default rather than tuned. Chunking uses lossless character windows, at the cost of poor structural
boundaries and no token-aware optimization.

The fake embedding space uses deterministic hashed word counts; the OpenAI space encodes
provider, embedding model and dimension. Vector retrieval filters space as well as tenant
to avoid incompatible embedding comparison. Lexical retrieval intentionally remains usable
across spaces. Reembedding/model lifecycle is deferred. The SDK is official and uses
schema-constrained Responses output, explicit per-call deadlines, request deadlines and
zero retries; this bounds failure latency at the cost of transient-error availability.

Phase 3 installed manual OpenTelemetry traces/metrics and background OTLP/HTTP export;
Phase 4 separates configured and served model identifiers. Allowlists exclude content and
credentials; telemetry errors do not fail product requests. Unknown usage/cost stay unknown.
Framework-native telemetry is disabled to keep one controlled export boundary.

The answer/planner schemas contain constrained strings, including minLength/maxLength.
Mocked SDK acceptance does not establish the real API's support. The opt-in smoke tests the
actual schemas with at most nine reserved requests; acceptance/rejection remains
NOT EXECUTED — CREDENTIALS NOT PROVIDED. See [live status](../evidence/release/live-provider-status.md).
