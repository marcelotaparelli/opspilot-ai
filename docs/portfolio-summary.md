# OpsPilot AI — portfolio summary

## Problem

Operators need to find runbooks within their tenant and turn findings into tracked work without
giving a language model authority over identity, approvals or external writes.

## Architecture

FastAPI validates contracts and resolves bearer tokens to principals. Domain/application ports
separate PostgreSQL, OpenAI and GitLab adapters. PostgreSQL holds text, 256-dimensional pgvector
embeddings, full-text indexes and agent state. A bounded LangGraph topology controls planning.
Forced RLS, tenant-filtered SQL and application context checks enforce separate boundaries.

## AI Engineering

RAG uses character-window chunking, exact cosine retrieval, lexical OR queries with `ts_rank`
and RRF. Embedding-space identifiers prevent incompatible vector comparisons. Structured
answers are independently validated against authorized citation IDs; uncited output abstains.
The agent can search, draft one issue or answer. OpenAI adapters use explicit deadlines,
zero retries and `store=False`. A deterministic fake supports reproducible local evaluation.

## Evaluation

Retrieval-v1 was too trivial to distinguish strategies. Retrieval-v2 introduced hard negatives,
paraphrases, identifiers, ambiguity, multiple relevant documents and a second tenant: 24 dev
and 36 once-consumed held-out queries. Historical held-out MRR@5 was lexical 0.7532, vector
0.4375 and hybrid 0.6306 with the fake embedder. Lexical outperformed hybrid; this does not
measure semantic embeddings or real answer quality. CI uses dev regression, never held-out.
[Evaluation](evaluation/retrieval-v2.md).

## Agent Safety

The model cannot supply tenant identity, approvals or arbitrary projects. A distinct approver
accepts the SHA-256 hash of the exact stored action. Execution rechecks current policy and
claims persisted work. Historical agent evaluation passed 16/16 cases with real PostgreSQL,
scripted/offline planners and fake GitLab; unauthorized actions were 0/16, approval bypasses
0/16 and duplicate side effects 0/5 executing cases.
[Evidence](evidence/phase3/agent-eval-v1.json).

## Observability

Manual OpenTelemetry traces, bounded metric labels and JSON logs correlate requests/runs.
Allowlists exclude content and secrets. Token usage and configured price estimates retain
unknown values instead of treating them as zero. Phase 4 records served model IDs separately.
Export failure is isolated from product logic; the local Collector/Jaeger profile is optional.
[Signals and historical load](observability.md).

## Reliability

Versioned migrations run transactionally under an advisory lock. Tenant context is local to
transactions, including pool reuse. Durable proposals/approvals/executions support restart
recovery. Lost GitLab responses become ambiguous; marker reconciliation precedes bounded
resend. Historical tests exercised process death, concurrent approval/resume and DB failure
following an external write. There is no exactly-once guarantee or automatic recovery worker.

## Deployment

The Terraform blueprint describes public ALB → Fargate → private RDS/pgvector, immutable ECR,
Secrets Manager and optional ADOT. Runtime declarations include UID 10001, read-only root
and dropped capabilities. NAT is optional. Four deliberate IaC findings are documented with
risk/mitigation. AWS deployment remains unverified; the local restricted runtime passed.
[Blueprint and cost drivers](deployment/aws.md).

## Results

Approved Phase 3 records 201 unit and 64 integration tests, 10/10 security cases and 14/14
mutation checks after fixing a broken baseline. Phase 4 passed 241 unit and 72 integration
tests, migrations/regressions, observability, restricted Docker runtime, new-volume HTTP,
scans, offline Terraform and full cold clean-room. Repeated CLI logging now survives closed
capture streams. The image has zero fixable HIGH/CRITICAL findings, while 44 unfixed HIGH
findings remain recorded. Engineering release and clean-room PASS; v0.1.0 is published on
GitHub, and hosted GitHub Actions jobs `checks` and `terraform` PASS. Terraform remains a
validated blueprint, not applied in production.

Post-release [OpenAI live integration evidence](evidence/release/live-openai-smoke.json)
passed on 2026-10-04 using `text-embedding-3-small` (256 dimensions) and `gpt-4.1-mini`.
Strict answer/planner schemas, database-backed RAG (1 evidence item / 1 citation), citation
membership, usage, served models, trace IDs and bounded timeout passed. No secret was found
in captured logs/spans. Pricing was not configured; no measured cost is claimed.

Post-release [GitLab live integration evidence](evidence/release/live-gitlab-smoke.json)
passed on 2026-10-05 in a real sandbox: zero GitLab requests before approval, successful
creation/GET, both markers present, second approval 409 and resume succeeded with 1 matching
issue / 1 create POST. Closure was confirmed with final state closed. This does not establish
exactly-once or production GitLab deployment.

## Limitations

GITLAB LIVE EVIDENCE: PASS — POST-RELEASE SANDBOX INTEGRATION ONLY. OpenAI live evidence covers
the exercised integration and schema acceptance only; semantic retrieval quality, factual
entailment and AWS performance remain unmeasured. Static tokens lack SSO/lifecycle, recovery
needs client invocation, exact vector search has linear work, and the default cloud topology
has limited redundancy.
