# Live provider evidence

OPENAI LIVE EVIDENCE: PASS — POST-RELEASE INTEGRATION ONLY

GITLAB LIVE EVIDENCE: NOT EXECUTED — CREDENTIALS NOT PROVIDED

## OpenAI post-release record

The owner executed `scripts/live_openai_smoke.py` against real OpenAI and real PostgreSQL
migrated to schema v2. The retained [JSON](live-openai-smoke.json) records
`2026-10-04T23:45:24+00:00` and `passed: true`. This evidence is subsequent to the
Phase 4/v0.1.0 gate; the historical gate records and published tag/release are unchanged.
Only the successful final artifact is retained as the live evidence.

| Observation from the JSON | Result |
| --- | --- |
| Configured / discovered models | `text-embedding-3-small` and `gpt-4.1-mini` |
| Real embedding | PASS: 256 dimensions, expected 256 |
| Strict structured answer | PASS: `ModelAnswer`, `schema_accepted: true`, citation membership true |
| Planner structured output | PASS: `Envelope(decision union)`, `schema_accepted: true`, tool `search_knowledge` |
| Database-backed RAG end-to-end | PASS: 1 retrieved evidence item, 1 citation; evidence and citation membership checks true |
| Usage | PASS: 7 operation records; 6 successful operations have token counts; timeout usage remains null |
| Served models | PASS: `text-embedding-3-small` and `gpt-4.1-mini-2025-04-14`; timeout served model remains null |
| Traces | PASS: `trace_id_present: true` in all 7 operation records; actual trace IDs are not exported in this JSON |
| Timeout | PASS: `timed_out: true`, `elapsed_ms: 2`, step `ms: 9`, usage error type `timeout`; classified/bounded check true |
| Secret exclusion | `secret_in_logs_or_spans: false` for captured logs/spans |
| Pricing / cost | `pricing_configured: false`; every `estimated_cost_usd` is null; no measured cost |
| Request reservation | 9 reserved / 9 budget; reserved upper bound is not a measured request count |

All six steps have status `ok`, and all eight evidence checks are true. The cost predicate
is conditional on configured pricing; its true value does not establish cost capture here.
The exercised strict answer and planner schemas were accepted by the real API for these
models in this run, including the answer schema's minLength/maxLength constraints. This is
integration evidence only: no semantic retrieval/answer quality, factual entailment or
real-model quality evaluation is claimed. The held-out split was not rerun.

Credential values, sensitive request payloads and `.env` are not evidence artifacts.
The scripts require explicit allow flags and complete configuration; missing configuration
must exit 2 before network work. This documentation update did not repeat OpenAI calls.

## GitLab next live smoke

GitLab's smoke remains unexecuted. `scripts/live_gitlab_smoke.py` requires HTTPS, a disposable
sandbox project ID, a project access token with Reporter role / `api` scope / short expiry,
a real database migrated to schema v2, and `OPSPILOT_ALLOW_REAL_GITLAB_SMOKE=true`.
[Workflow and invocation requirements](../../architecture/agent-workflow.md#13-optional-real-gitlab-smoke-not-executed).
It exercises proposal/approval/create/GET, second approval 409, resume without duplicate, then
close. Partial failure cleanup attempts marker lookup if no IID was received. Attempts are
counted even with lost responses; unknown response status remains unknown. Search/cleanup can
fail, so sandbox leftovers still require operator reconciliation. No real GitLab call was
made for this documentation update; permissions, search visibility and cleanup remain unverified.

Rehearsals use mock OpenAI transport and fake GitLab with real PostgreSQL. Final full
unit/integration rehearsals and clean-room passed, including negative evidence and
lost-response cleanup regressions. They remain distinct from live evidence.
