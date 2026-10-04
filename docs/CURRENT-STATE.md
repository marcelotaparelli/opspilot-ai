# Current state — Phase 4 release candidate

ENGINEERING RELEASE: PASS

OPENAI LIVE EVIDENCE: NOT EXECUTED — CREDENTIALS NOT PROVIDED

GITLAB LIVE EVIDENCE: NOT EXECUTED — CREDENTIALS NOT PROVIDED

PUSH REALIZADO: NO

All critical local gates and complete clean-room passed on 2026-10-04. Final source was
validated from base HEAD 55ddc102bef685d103f4868afea782cd1c5dbb88 on main. The sole conditional
release commit uses `chore: prepare opspilot release candidate`; resolve SHA/author with
`git log -1 --format=fuller`. Origin: git@github.com:marcelotaparelli/opspilot-ai.git.
No push, tag, fetch/pull, AWS apply or Phase 5 work.

Phase 4 adds production configuration checks, deliberate docs exposure and safe errors,
served-model telemetry, opt-in live smokes/rehearsals, migration paths and rollback tests,
pinned image/tooling/CI, scanner evidence, AWS Terraform and release documentation.
The final resumption fixed CLI handlers flushing closed streams across repeated calls.

241 unit and 72 real PostgreSQL integration tests pass without skips, as do 5 migrations,
RAG DEV, 16/16 agent, 10/10 security, telemetry/fault/rehearsals and local production mode.
Image/runtime, cold reproduction, new volumes, HTTP tenant/approval checks, secret control,
SBOM/audits and 56/58/53-resource offline plans pass. Full image findings remain visible:
44 HIGH, 60 MEDIUM, 60 LOW, 2 UNKNOWN, none fixable. Four IaC risks are explicitly classified.

[Final evidence](evidence/release/final-validation.md), [gate record](evidence/release/final-gates.json),
[clean-room](evidence/release/clean-room-validation.md). Earlier blocked handoffs are historical.
Both providers remain unverified live; held-out retrieval was not evaluated. No real-model,
AWS runtime/TLS/SQL-log privacy/performance or exactly-once guarantee is implied. Static
identity, manual recovery, narrow defaults and known blueprint risks remain documented.
