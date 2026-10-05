> HISTORICAL ATTEMPT. The provider statuses below describe this attempt. See [final validation](final-validation.md) for the engineering gate and [current post-release provider status](live-provider-status.md) for subsequent live evidence.

# Phase 4 — latest revalidation handoff

ENGINEERING RELEASE: BLOCKED

OPENAI LIVE EVIDENCE: NOT EXECUTED — CREDENTIALS NOT PROVIDED

GITLAB LIVE EVIDENCE: NOT EXECUTED — CREDENTIALS NOT PROVIDED

PUSH REALIZADO: NO

This attempt preserved HEAD `55ddc102bef685d103f4868afea782cd1c5dbb88` and the complete
working tree. No staging, commit, Git identity change, fetch/pull/push, volume removal,
Terraform application or Phase 5 work occurred. Environment bootstrap failures are not
product test failures. Previous handoff executions remain historical.

## 1. Environment and tools

Root: `/workspace/opspilot-ai`; `/workspace` is not the Git root. Debian 12, x86_64,
Linux 6.12.98, UID/GID 0 inside the managed sandbox, 7.8 GiB RAM, approximately 4 GiB free
workspace disk. Root identity does not grant access to forbidden sockets.

| Tool | Observed | Project target / outcome |
| --- | --- | --- |
| Git | 2.39.5 | usable |
| Python | `python` absent; `python3` 3.12.10 | pinned 3.12.15 unavailable |
| uv | 0.7.3 | CI/image pin 0.12.23 unavailable |
| Ruff | cached 0.11.9 | matches project pin; executable |
| Docker / Compose / buildx | 29.8.2 / 5.6.0 / 0.37.1 | daemon access denied |
| Terraform / Trivy / Gitleaks | absent | pins 1.16.5 / 0.75.0 / 8.30.1 |

GitHub/PyPI DNS resolution failed. Curl downloads from GitHub, PyPI and
releases.hashicorp.com failed with exit 6. Creating a socket raised PermissionError errno 1.
Docker socket access was denied. `uv python install 3.12.15` reported no matching download
entry in old uv. Downloads of pinned uv, Terraform, Trivy and Gitleaks failed before download;
no archive was installed without checksum verification. Requirements/lock were not changed.

## 2. Audit of the seven previous Codex changes

| Change | Source findings; runtime validation remains pending |
| --- | --- |
| OpenAI PASS | Usage, served model, configured pricing, traces and RAG evidence influence PASS. |
| Timeout | Requires timeout classification and bounded duration; generic provider error is insufficient. |
| GitLab counts | Entry reserved before await; lost response counts once with unknown status, without retries. |
| Cleanup | Substring lookup alone did not establish ownership; hardened below. |
| CLI exceptions | Class-only failed JSON; no exception text/traceback. Embedded provider credentials cause output refusal. Broad catches cannot return PASS. |
| Compose | APP_ENV/EXPOSE_API_DOCS forwarded; all three configuration variants parse. |
| CI artifacts | `if: always()` retains generated reports after blocking scans fail. It cannot upload files from steps never reached. Earlier quality/dependency failures can prevent image scans. |

API/provider/telemetry diffs were reviewed for sanitized errors, served-model span fields and
framework telemetry disablement. Terraform declares private encrypted RDS, TLS13/TLS12 ALB
policy, immutable ECR, non-root/read-only containers, dropped capabilities and resource limits.
No secret version/value resource or local state/plan binary was added. These are source audits.

## 3. Additional bugs and evidence gaps corrected

* OpenAI skipped optional RAG whenever a DSN contained substring `unused`. It now compares
  only the reserved no-database sentinel. A new rehearsal asserts that a configured password
  containing `unused` reaches the DB path instead of recording a skip.
* GitLab cleanup accepted smoke-marker substring hits. It now searches the approved action
  key, GETs candidates and requires matching project, IID and complete approved description
  with exact action footer before closing. Normal close also verifies ownership. New cases
  cover mismatched fields and an unrelated issue quoting the marker after a lost response.
* Returned adapter counts omitted reconciliation attempted during cleanup; the report now
  refreshes that snapshot afterward.
* GitLab PASS now requires expected create/approval/close/confirmation HTTP statuses and
  ownership of the final fetched issue, alongside prior safety checks.
* Repository-wide link checking found a wrong relative evidence link in the historical
  Codex-to-Claude handoff; only its path was corrected, preserving the historical record.

Full mypy/pytest validation of these changes is pending. A copied marker alone is refused;
an exact malicious copy of all checked fields still requires a trusted disposable sandbox.
Remote edits between GET and PUT cannot be made atomic by this client.

## 4–12. Quality, tests, migrations, regression and rehearsals

FINAL static results reexecuted here: Ruff format PASS (64 files), lint PASS, AST syntax
PASS (64 files; no imports/tests), three Compose parses PASS, CI source audit PASS,
JSON/TOML PASS, Git whitespace/integrity PASS.

Twenty-four assertions against extracted actual-source functions passed: refusal gates,
ownership and negative OpenAI evidence/timeout predicates. These supplemental checks are
not pytest, SDK rehearsals, network execution or live evidence.

Pinned lock check, `uv run python --version`, mypy, full unit and full integration stopped
before execution with exit 2: Python 3.12.15 missing. `verify.sh` stopped at its first lock
check. The previous alternate-version lock result is HISTORICAL.

| Required gate | Current result |
| --- | --- |
| Unit collected/passed/failed/skipped/deselected | all unknown; collection never began |
| Integration collected/passed/failed/skipped/deselected | all unknown; collection never began |
| PostgreSQL version / pgvector / applied schema | not observed; new Compose project denied |
| Empty→latest, v1→v2/latest, transaction failure | BLOCKED; latest=2 from source only |
| RAG DEV | BLOCKED; consumed held-out not rerun |
| Agent regression / unauthorized, bypass, duplicate rates | BLOCKED; no final measurements |
| Security regression / leak testing | BLOCKED |
| OTel / collector up, down, recovery | BLOCKED |
| Full live-smoke mock/fake rehearsals | BLOCKED |

No skip/xfail calls were found in checked test source; real-DB fixtures fail rather than
silently skipping missing DB settings. New regressions target the affected paths but have
no completed pytest result. Migration tests cover preservation, readiness, concurrency and
unknown versions; explicit DDL failure/rollback evidence is still required.

## 13–22. Docker, scans, Terraform, Compose and HTTP

No-cache image build exit 1: daemon permission denied. Final image ID/digest, size, observed
UID and read-only-root result are unknown. UID 10001, writable tmpfs and dropped capabilities
are declarations only. Final SBOM, dependency audit, image scan, working-tree/history Gitleaks
and scanner positive control are BLOCKED. No fresh vulnerability counts exist.

Inherited pip-audit and CycloneDX artifacts remain byte-for-byte unchanged, with historical
hashes/targets in `artifact-provenance.json`. They satisfy no final scan gate.

Terraform fmt/init/validate and default/NAT/collector-disabled plans are BLOCKED: tool absent,
download unavailable. Counts 56/58/53 are historical, not current plans. Fresh Trivy IaC is
BLOCKED. Public ALB, broad HTTPS egress, IAM DB authentication off and Performance Insights
off remain classified risks; no new scan establishes that these are the only findings.

Dedicated project `opspilot-phase4-revalidation` could not start DB. No volumes were removed
or reused. Empty-volume DB→migration→API, observability profile runtime, health/readiness,
HTTP RAG/tenant isolation and approval/execution/terminal agent against fake GitLab were
not executed.

## 23–27. CI, clean-room, docs, evidence and remaining limits

Static CI audit confirms lock, lint/types, unit/integration (including OTel/rehearsals), DEV,
agent/security gates, build, secret/dependency/image scans, SBOM and Terraform validation.
All four distinct Actions have 40-hex commit pins; scanner downloads have SHA-256 checks.
No live-provider, held-out or Terraform application command is present. Hosted jobs and
download checksums were not executed in this environment.

Clean-room is BLOCKED, not PASS. An isolated bootstrap precheck copied manifests/source to
a fresh temporary directory with no venv and empty uv cache; locked all-extras/dev sync
stopped at missing Python. This is not the complete clean-room sequence and did not change
the original venv. Prerequisite gates are not green. DNS/network restrictions prevent
attributing this bootstrap result to a product reproducibility bug.

Docs were reviewed for technical/stale/unsupported claims, retaining historical measured
results with their scope. Only corrections/provenance were updated. AWS qualitative drivers
remain Fargate, RDS, ALB, NAT, endpoints, logs/telemetry, OpenAI and egress; no monthly price
is invented. NAT stays optional under the documented public-task/ALB-SG egress tradeoff.
`final-gates.json` separates reexecuted static checks, historical results and blocked gates.
Exact tooling, services, all scans and full clean-room remain prerequisites for commit.

## 28–32. Live providers, Git and commit

No OpenAI key, complete GitLab credentials or live allow flags were present. No real provider
call or credential request occurred. Both live statuses remain exactly NOT EXECUTED —
CREDENTIALS NOT PROVIDED; missing credentials alone do not block engineering release.

Origin: `git@github.com:marcelotaparelli/opspilot-ai.git`. HEAD/five-entry log unchanged;
fsck PASS. Tree intentionally dirty, no deletion or staged file; final inventory in
`files-changed.json`. Phase 4 commit SHA/message/author: not applicable, no commit created.
Intended message remains `chore: prepare opspilot release candidate`, author Marcelo Taparelli
`contato@marcelotaparelli.com.br`, conditional on all critical gates and clean-room PASS.

PUSH REALIZADO: NO
