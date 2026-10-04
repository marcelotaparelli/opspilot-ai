# Phase 4 — final engineering validation

ENGINEERING RELEASE: PASS

OPENAI LIVE EVIDENCE: NOT EXECUTED — CREDENTIALS NOT PROVIDED

GITLAB LIVE EVIDENCE: NOT EXECUTED — CREDENTIALS NOT PROVIDED

PUSH REALIZADO: NO

Validated on 2026-10-04, from base HEAD `55ddc102bef685d103f4868afea782cd1c5dbb88`.
The final source is bound by [SHA-256 manifest](validated-source-manifest.json).
[Commands, timestamps, exit codes and retained logs](execution-records.json) and
[gate ledger](final-gates.json) are the authoritative current evidence. Earlier handoffs are historical.

## Environment and corrections

The first requested environment command found only `XDG_CACHE_HOME=/root/.cache`, rather than
the seven requested values. Every validation process then received the seven exact paths under
`/workspace/.opspilot-runtime` through an explicit process wrapper; they were rechecked there.
The parent Codex environment was not retroactively changed. Initial free space: /tmp 505 MiB,
/workspace 4.0 GiB, /mnt/docker-data 3.3 GiB. No storage failure occurred in this resumption.
Docker 29.8.2, overlayfs, root /mnt/docker-data; Terraform 1.16.5, Trivy 0.75.0, Gitleaks 8.30.1.
Python 3.12.15 and uv 0.12.23 were installed without changing project pins. The uv archive's
published checksum matched `9167d72b3319674b6303c4cbe071854bba13ebdf3d76b1a7cbdc175471fb66d6`.

The real CLI defect was fixed with a scoped console-handler replacement that routes logs to
stderr and restores the original handlers on success or exception. It does not flush borrowed
closed streams, close caller-owned streams or redirect file handlers. Regression tests exercise
both CLI orders, four invocations per order, an already-closed original stream and subsequently
closed stderr captures; JSON remains separate and failure messages stay redacted.

A real SQL-failure migration test now proves that v1/v2 DDL and the schema-version table roll
back together; retry with the original migrations succeeds. Initial formatting/type issues in
the new helper were corrected before the final quality runs and clean-room.

Two external diagnostic probes initially assumed a legacy Jaeger endpoint/metric name and a
noncanonical database role. Corrected probes use Jaeger v3 query fields and the required
`opspilot_app` runtime role. These were probe configuration failures; no application change
or guard weakening was made to pass them. The production probe used only ephemeral test
credentials and restored the disposable DB's original runtime password afterward.

## Completed gates

| Gate | Measured result |
| --- | --- |
| Lock / installation | 70 lock entries; pinned locked dev/all-extras installation, repeated cold |
| Ruff / strict mypy | 64 files, PASS |
| Unit | Final clean-room: 313 collected, 241 passed, 72 deselected, 0 failed/skipped |
| Integration | Original and clean-room: 313 collected, 72 passed, 241 deselected, 0 failed/skipped |
| Migrations | 5/5: empty/idempotence/RLS/readiness, preserved v1 data, future-version refusal, concurrency, failed-DDL rollback/retry |
| Retrieval | v1 measurement and retrieval-v2 DEV gate PASS, fake embeddings/real PostgreSQL; held-out never evaluated |
| Agent | 16/16, zero unauthorized/bypass/duplicate side effects in exercised cases |
| Security | 10/10, including logs/spans leak checks |
| Observability / rehearsals | Full unit/integration coverage; real local Collector/Jaeger metrics/traces, down/recovery HTTP; mock OpenAI and fake GitLab only |
| Production runtime | Production + fake provider ready; docs/OpenAPI/redoc/unknown route 404 with safe envelope |
| Docker | No-cache release and clean-room builds; UID/GID 10001, read-only root, writable tmpfs, zero effective caps, no-new-privileges |
| Compose / HTTP | Fresh volume DB healthy → migration exit 0 → API healthy; RAG/tenant and approval/execution/terminal smokes, repeated clean-room |
| Dependencies | pip-audit 2.9.0, 67 packages, zero reported vulnerabilities |
| Image scan / SBOM | Full report retained: 166 findings; {'LOW': 60, 'HIGH': 44, 'MEDIUM': 60, 'UNKNOWN': 2}; no fixable findings, fixable HIGH/CRITICAL gate PASS; CycloneDX for exact release image |
| Secrets | Working tree/history and clean snapshot clean; positive control detected under repository policy |
| Terraform | fmt, readonly-lock no-backend init, validate; default/NAT/no-collector 56/58/53 creates; checked known plan invariants; no apply |
| IaC | 90 successful checks, four classified risks: AWS-0053 HIGH, AWS-0104 CRITICAL, AWS-0133 LOW, AWS-0176 MEDIUM; no suppressions |
| CI | Static workflow audit and 12 regression-gate tests PASS; hosted run NOT EXECUTED |
| Clean-room | Empty dependency cache, fresh venv, full quality/suites/regressions, migrations, no-cache build, recreated disposable volume, HTTP and runtime proof PASS |

Release local manifest digest: `sha256:a09a2ab0988232a3b8b6d82be22bf667352755e83b7c770b37a68079371a27ab`; config digest: `sha256:a09a2ab0988232a3b8b6d82be22bf667352755e83b7c770b37a68079371a27ab`;
size 362790524 bytes. This is a local Docker manifest, not evidence of registry publication.
The independently rebuilt clean-room image has a different digest; source/dependency equivalence
and functional reproduction are proven, not byte-identical image builds (apt updates/timestamps vary).

## Scope and remaining limitations

OpenAI/GitLab live calls, hosted Actions, AWS deployment and retrieval-v2 held-out measurement
were not executed. Missing provider credentials do not block the engineering gate and were not
requested. No provider/model-quality, real permission/search/cleanup, AWS ADOT/IAM/pgvector/TLS,
production SQL-log privacy or cloud performance claim is made. Static tokens, manual recovery,
search-based reconciliation without exactly-once guarantees and single-replica/single-AZ defaults
remain documented limitations. Unfixed image findings and four deliberate IaC risks remain visible.

Documentation and evidence were finalized after the clean-room run. All 105 non-document
source/configuration/dataset files match its manifest. The narrow Gitleaks fingerprint exception
was copied into the snapshot and rescanned; no application, dependency, image or Compose input
changed after validation. Final tree/history scans and diff/integrity checks precede the sole
conditional commit, `chore: prepare opspilot release candidate`, authored by Marcelo Taparelli
<contato@marcelotaparelli.com.br>. Obtain its SHA with `git log -1`; no push or tag is performed.
