# Validation records and provenance

ENGINEERING RELEASE: PASS

OPENAI LIVE EVIDENCE: PASS — POST-RELEASE INTEGRATION ONLY

GITLAB LIVE EVIDENCE: NOT EXECUTED — CREDENTIALS NOT PROVIDED

Release v0.1.0 is published on GitHub, tagged at
`04fb9aada0c11f16838544316c1fd71e6537d911`. The owner confirmed GitHub Actions completed
successfully on a hosted runner: jobs `checks` and `terraform` PASS. See [current state](CURRENT-STATE.md).

[Final Phase 4 validation](evidence/release/final-validation.md) and
[execution records](evidence/release/execution-records.json) bind current source, commands,
timestamps, retained logs, exact image metadata, scanners and full clean-room reproduction.
Those records preserve the pre-publication local/static-CI scope. Hosted CI PASS and publication
were subsequently confirmed by the owner; this documentation task does not rerun validation.

| Record | Scope |
| --- | --- |
| [Phase 1](evidence/validation-summary.json) | Historical 98 unit / 15 integration, 24/24 injected defects, Docker/HTTP |
| [Retrieval-v2](evaluation/retrieval-v2.md) | Historical 24 DEV / 36 once-consumed held-out synthetic queries; held-out not rerun |
| [Phase 2](evidence/phase2/validation-summary.json) | Historical approval/recovery/concurrency, 16 agent cases and 17/17 mutations |
| [Phase 3](evidence/phase3/) | Historical 201 unit / 64 integration, 16 agent / 10 security, 14/14 mutations, telemetry/load |
| [Phase 4](evidence/release/README.md) | Historical release gate FINAL PASS: 241 unit / 72 integration, 5 migrations, regressions, scans, runtime, plans and cold clean-room |
| [Post-release OpenAI live smoke](evidence/release/live-openai-smoke.json) | PASS at 2026-10-04T23:45:24+00:00: real embedding, strict answer/planner schemas, database-backed RAG, citation membership, usage, served models, traces and bounded timeout; integration only |

Final suites each collected 313 tests: non-integration selected/passed 241, deselected 72;
integration selected/passed 72, deselected 241; no failures/skips. Telemetry and offline
live-smoke rehearsals are included. The source/config hash manifest matches the clean-room;
final documentation/result artifacts were generated afterward. Earlier handoffs are historical.

## Reproduce

Use pinned Python 3.12.15/uv 0.12.23, a working Docker daemon, disposable PostgreSQL 17/pgvector
0.8.0 and separate runtime/admin URLs. Install the lock from an empty dependency cache.
Execute verify.sh's quality/suite stages and RAG DEV/agent/security runners/gates; never evaluate
the consumed held-out split. Build without cache, record exact metadata/SBOM and the full image
scan, then enforce fixable HIGH/CRITICAL separately. Keep the unfixed findings visible.

Use dedicated projects/volumes, DB healthy → migration exit 0 → API healthy. Assert non-root,
read-only root, writable tmpfs, dropped capabilities, health/readiness, HTTP tenant isolation
and approval/execution against fake GitLab. Test the optional Collector/Jaeger profile separately.
Run Terraform fmt/no-backend readonly-lock init/validate and three offline placeholder plans;
retain known plan invariants and all four IaC risk classifications. Never apply infrastructure.

Scan working tree/full history and verify a synthetic control outside Git. Recreate a snapshot
without .git/.env/venv/cache/runtime; repeat install, quality/suites/regressions, migrations,
no-cache Docker and new-volume HTTP. [Executed clean-room](evidence/release/clean-room-validation.md)
passed these stages individually; verify.sh was not invoked as a single command.

Live evidence is separate from the historical engineering PASS. The post-release OpenAI JSON
records `passed: true`, `text-embedding-3-small` / `gpt-4.1-mini`, 256-dimensional embeddings,
strict answer/planner schema acceptance, 1 retrieved evidence item and 1 citation, usage, served
models and trace IDs. Timeout classification/bounds passed; `secret_in_logs_or_spans: false`.
`pricing_configured: false` and all cost values are null; no measured cost is claimed.
GitLab permissions/search/cleanup and AWS runtime remain NOT EXECUTED. Hosted Actions passed
for v0.1.0; Terraform validation in CI does not establish a production deployment.
No new mutation score or real semantic-quality claim; this update did not rerun live providers.
