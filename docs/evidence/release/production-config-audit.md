# Production configuration / migration audit

Final local unit/integration, read-only container and [production-mode probe](production-runtime.json) PASS. AWS deployment remains untested.

| Control | Source behavior / remaining verification |
| --- | --- |
| Public API surface | `/health` and `/ready` are public; API docs/OpenAPI default off. Explicit development opt-in; production rejects it. Errors use one safe envelope, including routing 404/405. |
| Environment | Compose now forwards `APP_ENV` and `EXPOSE_API_DOCS`; Terraform forces production. Example marker/HTTP GitLab checks fail startup; these checks are not entropy/complete credential validation. |
| Provider | OpenAI selection requires a nonempty key. Default local provider is fake; AWS default selects OpenAI. Real models/schema/usage unverified. |
| Identity/policy | Tokens 32–128 ASCII chars; legacy UUID principals have agent role; approvers need explicit principals/distinct subject. Static identity has no SSO/rotation service. |
| Timeouts | Request default 60s, agent 45s, DB 5s, provider 15s, planner 20s, GitLab 10s, lease 60s. Agent deadline < request; lease > GitLab request. Production ALB margins assume defaults. |
| DB role/readiness | Rejects superuser/BYPASSRLS and incorrect schema/RLS; transaction-local context and tenant predicates. Real DB/connection-pool integration PASS. |
| Secrets | SecretStr and hidden DSN parameters; Terraform has secret containers without versions/values. ECS execution role reads both app and migration secrets; per-task split is future hardening. |
| Telemetry | Manual allowlists, background export, framework automatic telemetry disabled. Served model span-only. AWS ADOT and DB log privacy unverified. |
| Runtime | Image/Compose/API task declare UID 10001/read-only root; Compose drops ALL and no-new-privileges; task declares drop ALL and resource limits. ADOT explicitly uses UID 4317 and env configuration URI. Local UID/read-only/tmpfs/capability proof PASS; AWS tasks remain undeployed. |
| HTTP GitLab | Development override only; production rejects it. HTTPS URL validation and no redirects. Partial-failure smoke cleanup is best effort, not guaranteed. |

Migration source: latest schema is 2; transactional advisory lock serializes callers. New tests
cover empty→v2/idempotence/readiness/forced RLS, v1→v2 preserving a document, rejection of
future version, and concurrent migrators. All five cases passed against real PostgreSQL, including a new injected-DDL failure/rollback/retry case, and repeated clean-room.
Do not report migration validation PASS from source reading. RDS bootstrap requires adapting
local superuser role setup to managed-admin permissions and checking pgvector availability.

Remaining config limitations: `max_execution_attempts` defaults to 3 in Settings and has no
`from_env` override; the Python setting is bounded but no undocumented environment knob is
claimed. Production checks do not force DSN TLS/certificate identity; RDS declares forced SSL,
and the deployment must validate client trust separately. RDS slow-query logs may include bound
content; audit database logging before sensitive ingestion. Collector/migration writable-path
behavior and IAM grants require an executed deployment review.
