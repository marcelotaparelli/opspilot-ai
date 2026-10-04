> **SUPERSEDED / HISTORICAL.** Not the current state. Kept for audit history only.
> Current measured record: [docs/VALIDATION.md](../VALIDATION.md).

# Execution record and blocked gates

These are observed results, not predicted outcomes. Full command/error records are in
[validation.json](codex-blocked-validation.json). The implementation sandbox is Debian 12 with Node
available. It has no Python/Python3, uv, Ruff, mypy, pytest, PostgreSQL, Docker or Git
executable. There are no cached copies in the inspected locations. Shell HTTPS access
failed with DNS `EAI_AGAIN`; direct-IP network access failed with `EPERM`. Installing
toolchains or starting the database was therefore unavailable in this workspace.

| Check attempted | Exact exit | Outcome |
| --- | ---: | --- |
| `uv lock` | 127 | Blocked: `uv: command not found`; no lockfile generated |
| `uv run ruff format --check .` | 127 | Blocked: uv absent; formatter did not run |
| `uv run ruff check .` | 127 | Blocked: uv absent; Ruff did not run |
| `uv run mypy` | 127 | Blocked: uv absent; type checker did not run |
| `uv run pytest -m 'not integration'` | 127 | Blocked: unit/API/SDK tests did not run |
| `uv run pytest -m integration` | 127 | Blocked: no tests ran; real PostgreSQL/pgvector unavailable |
| `uv run pytest -m integration tests/test_integration.py -k isolation` | 127 | Blocked: adversarial real-database test did not run |
| `uv run python -m opspilot.evaluation --seed --k 5` | 127 | Blocked: no evaluation results generated |
| `docker build --tag opspilot-ai:phase1 .` | 127 | Blocked: `docker: command not found` |
| `docker compose --env-file .env.example config --quiet` | 127 | Blocked: Docker absent; Compose validation did not run |
| `python3 -m compileall -q src tests` | 127 | Blocked: `python3: command not found` |
| `git init -b main` | 127 | Blocked: `git: command not found`; see repository creation below |
| `bash -n scripts/init-db.sh scripts/verify.sh` | 0 | Passed shell syntax only |
| `sh -n scripts/init-db.sh scripts/verify.sh` | 0 | Passed POSIX shell syntax only |
| Node dataset JSON/reference validation | 0 | Five documents / three cases; relevant IDs exist in their declared tenants |

Node's native filesystem/crypto/zlib modules were used to inspect files, parse and
validate the dataset JSON references, calculate the versioned fixture UUIDv5 and create
the local Git repository. JSON/static structure checks do not replace Python compilation,
Ruff, mypy, integration tests, evaluation or Docker build.

Because Git is absent, the final local commit is written as standard Git blob/tree/commit
objects with zlib compression, SHA-1 object identifiers, a v2 index, `main` reference and
HEAD. Author/committer is `Codex <codex@local.invalid>`. Object hashes, committed file
content, executable modes, index checksum and a single root commit are independently
verified with Node. Git CLI `status`, `fsck` and `show` could not run. No remote is configured,
no push was made and no external application was modified.

## Remaining acceptance work

The Phase 1 implementation is not fully validated. Resolve and commit a real `uv.lock`
with uv in a network-enabled Python environment and run every gate. CI deliberately fails
early without a committed lock; Docker also requires that file for a frozen install.
Format/type/test failures may still exist and must be fixed
based on execution. No passing test counts, retrieval quality, latency, load or deployment
claims are made. This commit preserves the implementation and explicit blocked checks.
