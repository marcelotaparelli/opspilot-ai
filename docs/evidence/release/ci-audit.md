# CI final source audit

Workflow: `.github/workflows/ci.yaml`; static audit only, no GitHub-hosted execution/push.

Present: lock check/locked sync; Ruff format/lint; strict mypy; full non-integration and real-DB
integration suites through `verify.sh`; fresh CI PostgreSQL/migration; retrieval-v1 measurement;
retrieval-v2 **dev** regression; agent regression; security regression; image build; real HTTP
RAG/agent against fake GitLab; working-tree/full-history Gitleaks; whole-lock pip-audit; complete
image vulnerability JSON plus fixable HIGH/CRITICAL blocking gate; CycloneDX SBOM; Terraform
fmt/no-backend locked-provider init/validate. Observability/release rehearsal tests are included
in the suites, not live provider steps.

All four distinct Actions use full commit SHAs (checkout, setup-uv, upload-artifact,
setup-terraform). Gitleaks/Trivy archives use pinned versions and SHA-256 verification.
Checkout uses `fetch-depth: 0`. Dependency audit includes dev groups, strict/hash checks.
Artifacts upload with `if: always()`; full image findings, SBOM and dependency report are
retained when their steps produce them, even if a blocking scan fails. A dependency failure
can prevent subsequent image scanning in that CI run; final local release must still finish
all scans before commit.

Absent from executable CI: live OpenAI/GitLab smokes, retrieval-v2 held-out runs, repository
provider secrets and infrastructure application. Terraform job has no real credential plan.
The scope-guard test exists in `tests/test_regression_gates.py`; its string checks establish
presence/absence, not that hosted CI steps actually succeeded. Static audit and config parsing
are not replacements for an executed workflow or clean-room validation.

Final local audit: 12 regression-gate tests PASS; complete clean-room independently executed
quality/runtime stages. This remains a static/source CI audit, not a hosted Actions result.
