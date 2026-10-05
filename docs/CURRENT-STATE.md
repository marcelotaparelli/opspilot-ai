# Current state — published v0.1.0

ENGINEERING RELEASE: PASS

OPENAI LIVE EVIDENCE: PASS — POST-RELEASE INTEGRATION ONLY

GITLAB LIVE EVIDENCE: PASS — POST-RELEASE SANDBOX INTEGRATION ONLY

RELEASE v0.1.0: PUBLISHED ON GITHUB

GITHUB ACTIONS HOSTED RUNNER: PASS — jobs `checks` and `terraform`

The owner confirmed publication and the complete hosted CI result after local release
validation. [Release v0.1.0](https://github.com/marcelotaparelli/opspilot-ai/releases/tag/v0.1.0)
is tagged at `04fb9aada0c11f16838544316c1fd71e6537d911`, the release commit
`chore: prepare opspilot release candidate`. All critical local gates and complete clean-room
passed on 2026-10-04. Origin: git@github.com:marcelotaparelli/opspilot-ai.git.
Terraform is a validated blueprint; no infrastructure was applied in production. This
post-release documentation task does not change v0.1.0 or perform another push.

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
[clean-room](evidence/release/clean-room-validation.md). These pre-publication records retain
their original no-push/static-CI scope; they do not describe the current GitHub release or
hosted CI status. Earlier blocked handoffs are historical.
The [post-release OpenAI smoke](evidence/release/live-openai-smoke.json), recorded at
2026-10-04T23:45:24+00:00, passed against real OpenAI and database-backed RAG. It used
`text-embedding-3-small` (256 dimensions) and `gpt-4.1-mini`, accepted both strict schemas,
and retrieved 1 evidence item with 1 citation. Usage, served models, trace IDs and a classified
bounded timeout passed; `secret_in_logs_or_spans` is false. Pricing was not configured, so
no measured cost is claimed. [Provider details](evidence/release/live-provider-status.md).
The [post-release GitLab smoke](evidence/release/live-gitlab-smoke.json), recorded at
2026-10-05T00:16:39+00:00, passed in a real sandbox: proposal 201 / awaiting_approval,
zero GitLab requests before approval, approval 200 / succeeded, create 201 and GET 200.
Both smoke/action markers were present. Second approval returned 409; resume returned
200 / succeeded, with 1 matching issue and 1 create POST. Close and confirmation returned
200, and final state was closed. Token scope is owner-reported in the provider details.
Held-out retrieval-v2 was not rerun for this release.
No real-model quality,
AWS runtime/TLS/SQL-log privacy/performance or exactly-once guarantee is implied. Static
identity, manual recovery, narrow defaults and known blueprint risks remain documented.
