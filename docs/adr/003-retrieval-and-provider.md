# ADR 003: Exact vectors, simple full-text search and deterministic RRF

Status: accepted for Phase 1; synthetic retrieval regression measured on 2026-10-04
(see [validation record](../VALIDATION.md)). Real model quality and load latency remain unmeasured.

Use exact cosine retrieval on 256-dimensional pgvector values and GIN-indexed PostgreSQL
`simple` full-text search, then RRF with constant 60. Fixed UUID tie breaks make a fixed
corpus repeatable. This avoids score calibration across fundamentally different ranking
scales. Four times K candidates per branch is a bounded initial heuristic, not a tuned value.

Exact vectors avoid approximate-search recall interactions with tenant filtering and make
isolation easier to reason about. They cost linear distance computation per tenant corpus;
no performance claim or ANN tuning is made. The simple dictionary is language-independent
but has no stemming and no stopword list. Because of that, AND semantics
(`websearch_to_tsquery`) made natural-language questions match nothing; validation replaced
it with OR over the question's word tokens, ranked by `ts_rank_cd` (no IDF, so stopword-only
overlap ranks low but can still match). RRF's 60 is the constant from Cormack, Clarke &
Buettcher (SIGIR 2009), kept as the `rrf()` default rather than tuned. Chunking uses lossless character windows, at the cost of poor structural
boundaries and no token-aware optimization.

The fake embedding space uses deterministic hashed word counts; the OpenAI space encodes
provider, embedding model and dimension. Vector retrieval filters space as well as tenant
to avoid incompatible embedding comparison. Lexical retrieval intentionally remains usable
across spaces. Reembedding/model lifecycle is deferred. The SDK is official and uses
schema-constrained Responses output, explicit per-call deadlines, request deadlines and
zero retries; this bounds failure latency at the cost of transient-error availability.

Only safe correlation/timing is installed. `observability.span` is the exact Phase 3
tracing extension; installing a tracer/exporter now would add scope without measured value.
