# Dependency decisions

Only direct necessities are added. No Python dependency was added or changed during
Phase 1 runtime validation (re-checked 2026-10-04: `uv lock --check` passes and `uv.lock` is unchanged;
PyYAML was used once via an ephemeral `uv run --no-project --with` to parse the CI workflow
and is not a project dependency). `pyproject.toml` pins direct versions; the real `uv lock` resolved 33 packages
including the project, and `uv sync --all-extras --dev` installed/audited 32 packages.
The committed `uv.lock` records versions, artifacts and hashes for runtime/development
dependencies. The actual [`uv tree --locked` output](evidence/dependency-tree.txt) was
reviewed. The isolated hatchling build dependency and its own transitives require separate
review; they are not part of that runtime/development lock. This is not a vulnerability audit.

| Dependency | Need / why stdlib is insufficient | Relevant transitives / maintenance impact |
| --- | --- | --- |
| LangGraph 1.2.12 (Phase 2) | Owner-mandated agent framework; used only for typed `StateGraph`, conditional routing, recursion limit and topology export ([ADR 004](adr/004-langgraph-control-flow-postgres-durability.md)). Checkpointer, `interrupt()`, prebuilt agents and LangChain tools are deliberately unused | Largest addition: the lock grew from 33 to 58 entries (+25). Via `langchain-core`: `langsmith` (requests, urllib3, charset-normalizer, requests-toolbelt, httpx2/httpcore2/httpx2-jsfetch, truststore, websockets, zstandard, orjson, uuid-utils), jsonpatch/jsonpointer, langchain-protocol, pyyaml, tenacity; `langgraph-checkpoint` (ormsgpack); `langgraph-sdk`; xxhash. LangSmith tracing is off unless `LANGSMITH_*`/`LANGCHAIN_TRACING*` variables are set; this project sets none. Upgrade LangGraph and langchain-core together and re-run the agent suite |
| HTTPX (runtime direct since Phase 2) | The GitLab adapter calls it directly (timeouts, no redirects, mock transport in tests). It was already in the lock through openai/langgraph; declaring it avoids relying on a transitive. Same pin 0.28.1, no new packages | anyio, httpcore, certifi, idna, h11 (already present) |
| FastAPI | ASGI HTTP routing, dependency injection and generated Pydantic contracts; stdlib has no comparable async API framework | Starlette, Pydantic, typing-extensions; follow framework/Starlette security updates together |
| Pydantic v2 | Bounded contracts, environment validation and SDK JSON schemas; dataclasses alone do not validate arbitrary HTTP JSON | pydantic-core native wheels, annotated-types, typing-inspection, typing-extensions; native parser updates and compatibility require locked upgrades |
| SQLAlchemy 2 with asyncio extra | Typed mature async engine, pooling, transactions, bound SQL and adapter lifecycle; stdlib has no PostgreSQL client/pool | greenlet native wheel, typing-extensions; dialect compatibility with asyncpg must be integration-tested |
| asyncpg | Actual asynchronous PostgreSQL protocol implementation; stdlib has no such driver | Native wheel / protocol code; PostgreSQL version and Python wheel compatibility are maintenance concerns |
| OpenAI official SDK | Required major-provider SDK behind ports; supplies structured Responses parsing and embedding contract support | HTTPX, httpcore/h11/certifi, anyio/idna/sniffio, Pydantic, distro, jiter native wheel, tqdm, typing-extensions; widest runtime dependency graph, confined to adapter, zero retries |
| Uvicorn without standard extras | ASGI server supporting lifespan and graceful shutdown; stdlib HTTP servers do not implement ASGI | click, h11; omits optional uvloop/watchfiles/websocket extras to reduce graph |
| HTTPX (dev direct) | Async ASGI and HTTP mock transports test the real API/SDK contracts without socket calls; stdlib lacks these transports | Also SDK runtime transitive; anyio/httpcore/certifi/idna; direct dev pin aligns mock interfaces with runtime SDK |
| pytest | Parametrized fixtures, failure reports and explicit real-integration markers; unittest would work but pytest is required and supports this fixture model | pluggy, packaging, iniconfig, platform-dependent colorama; dev-only execution surface |
| pytest-asyncio | Runs async tests with isolated event loops and async fixtures; pytest alone cannot await async tests | pytest; dev-only, fixture loop-scope behavior must be reviewed on upgrades |
| Ruff | Required formatter/linter, import and async rules; stdlib has no equivalent | Native binary wheels, no Python runtime dependency graph; dev/CI only |
| mypy | Strict static checking across source and tests; Python runtime does not enforce Protocol/annotation contracts | mypy-extensions, typing-extensions, native wheels; dev-only, stubs and library typing change on upgrades |
| hatchling (build) | Builds/installable src-layout wheel; Python stdlib has no PEP 517 wheel backend | packaging, pathspec, pluggy, trove-classifiers; build-only, isolated backend resolution must be reviewed in addition to runtime lock |
| uv (tool/container/CI) | Required dependency resolution, Python selection and frozen installs; stdlib venv does not resolve/lock dependencies | Rust binary and platform artifacts; pin 0.7.3, verify trusted distribution and update intentionally |

PostgreSQL 17 and pgvector 0.8.0 are infrastructure dependencies: PostgreSQL provides
transactional persistence, RLS and indexed full-text search; pgvector provides typed
vectors and cosine operators in the same authorization/transaction boundary. There is
no Python pgvector/numpy dependency: validated finite float arrays are JSON-encoded and
passed as **bound parameters**, explicitly cast to `vector` in SQL. PostgreSQL enforces
dimensionality. Numeric interpolation into SQL is never used.

Docker images and GitHub Actions also introduce supply-chain trust. Versions are selected
explicitly but not yet pinned to digests/commit SHAs. No dependency vulnerability audit
or current support guarantee was measured. CI credentials are ephemeral examples, and
no real provider credentials are used in tests. No auto-upgrade bot is installed.

Phase 2 alternatives considered: a hand-written loop (similar size, but no declared topology or
framework backstop; see ADR 004), the LangGraph Postgres checkpointer (adds psycopg plus a second
persistence model, so rejected), and `python-gitlab` (the three REST calls needed do not justify
another client and its dependency tree; httpx was already present).

Excluded: LangChain (beyond the transitive `langchain-core` LangGraph requires), LlamaIndex,
CrewAI, AutoGen, MCP SDKs, tokenizers, rerankers, NumPy, pgvector Python,
pydantic-settings, structlog, Alembic and OpenTelemetry. Stdlib suffices for chunking,
RRF, hashing, JSON, correlation, deadlines, logging and this single schema migration.
OpenTelemetry's extension point is documented in README rather than adding an exporter
and its dependency graph in this phase.
