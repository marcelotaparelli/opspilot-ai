# Dependency decisions

`pyproject.toml` pins direct dependencies; `uv.lock` contains 70 package entries including the
project and platform-conditional entries. That is not the count installed on every platform.
The lock is authoritative for versions, URLs and artifact hashes. Docker/CI use uv 0.12.23 and
Python 3.12.15; the application supports Python >=3.12,<3.14. Final installation and full
clean-room passed with Python 3.12.15 / uv 0.12.23, a new venv and empty dependency cache.
[Current evidence](evidence/release/final-validation.md).

| Dependency | Purpose / maintenance consequence |
| --- | --- |
| FastAPI 0.142.2 | ASGI contracts. Native telemetry is explicitly disabled; upgrade with Starlette and rerun privacy/API tests. |
| Pydantic 2.11.4 | HTTP/configuration/SDK validation; native core and generated schemas require compatibility testing. Domain values remain dataclasses. |
| SQLAlchemy asyncio 2.0.41 / asyncpg 0.30.0 | Transactions, pooling and PostgreSQL protocol; real integration tests cover dialect behavior, pool reuse and RLS. |
| OpenAI 1.78.1 | Official SDK confined to adapters; structured Responses parsing, embeddings, deadlines and zero retries. Transport mocks cannot prove live schema support. |
| HTTPX 0.28.1 | Runtime GitLab calls plus SDK/test transports; redirects disabled, bounded requests. A direct dependency rather than an incidental transitive. |
| Uvicorn 0.34.2 | Lifespan and graceful SIGTERM shutdown; standard extras omitted. |
| LangGraph 1.2.12 | Typed topology, routing and recursion bounds. App-owned PostgreSQL checkpoints; no framework checkpointer, prebuilt agent or LangChain tool layer. |
| OpenTelemetry API / SDK / OTLP HTTP exporter 1.45.0 | Manual traces/metrics and background export. Pins move together; HTTP avoids grpcio, but protobuf/exporter transitives add maintenance. |
| pytest 9.1.1 / pytest-asyncio 1.4.0 | Unit and marked real-database tests. Missing DB configuration fails required integration checks. |
| Ruff 0.11.9 / mypy 1.15.0 | Formatting/lint and strict types over source, scripts and tests; absent from runtime image. |
| hatchling 1.27.0 | Isolated PEP 517 wheel backend. Build dependencies require separate review beyond runtime lock auditing. |

Phase 4 upgraded FastAPI and pytest tooling and constrains transitive Starlette to >=1.3.1.
The final whole-lock audit reports zero known vulnerabilities in 67 audited packages;
see [audit provenance](evidence/release/vulnerability-summary.md). The image scan retains
166 unfixed findings, including 44 HIGH, with zero fixable HIGH/CRITICAL findings. Scanner
results describe current advisory coverage and do not establish vulnerability-free software.

LangGraph brings langchain-core, LangSmith, checkpoint/SDK and native serialization packages.
Only graph control flow is used. The project sets no `LANGSMITH_*` / LangChain tracing variables;
deployments should not introduce unrelated automatic export settings. OpenTelemetry adds
semantic conventions, protobuf/common protos and HTTP exporters. Unused transitive features
remain part of the locked dependency and attack surface.

PostgreSQL 17 / pgvector 0.8.0 provide RLS, full-text search and typed vector operators in one
transaction boundary. No Python pgvector or NumPy library is needed: finite validated vectors
are passed as bound SQL parameters. The migrator creates the vector extension. RDS engine and
extension availability must be checked before deployment; AWS behavior has not been tested.

uv/Python Docker stages are digest-pinned. The image installs the locked runtime graph,
updates Debian security packages and removes runtime pip. Apt updates vary over time: locks
and base digests do not make image bytes reproducible. Retain the final image digest, OS
package SBOM and scan together. Optional Compose database/Collector/Jaeger and fake GitLab
images still use version tags. The AWS collector uses a digest. Actions use commit SHAs;
Gitleaks 8.30.1 and Trivy 0.75.0 CI archives have SHA-256 checks; pip-audit is pinned to 2.9.0.
Advisory/database freshness still requires recording when scanners run.

Rejected additions: second vector database, Python GitLab wrapper for a few REST calls,
second checkpoint system, auto-instrumentation, second metric registry and larger telemetry
stack. No MCP, GraphRAG, multi-agent, frontend, Kubernetes, alternate AI provider, tokenizer,
reranker or new AI feature belongs to Phase 4.
