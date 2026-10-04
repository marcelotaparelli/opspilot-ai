# Clean-room validation — PASS

Run: 2026-10-04 21:57–22:00 UTC. Snapshot:
`/workspace/.opspilot-runtime/phase4/clean-room/snapshot`, 199 non-ignored working files at
creation. No `.git`, `.env`, venv, tool caches, database, volume or image was copied. Toolchains
were provisioned separately; Python 3.12.15 / uv 0.12.23 were retained. Dependency cache was
asserted empty before the run; a new venv was installed from the locked graph by download.
Pinned base images/advisory data are environment tooling, not inherited application state.

The first diagnostic clean-lock command accidentally targeted the original cwd; it is not
clean-room evidence. The cache was reset, and the recorded final clean-* commands all run
from the isolated snapshot, except the later external Docker runtime probe (which inspects
the clean-room containers and image).

Full sequence PASS: lock → cold locked dev install → Python version → Ruff format/lint →
strict mypy → 241 unit (0 failures/skips) → fresh PostgreSQL/pgvector → migration → 5 migration
path/rollback checks → 72 integration (0 failures/skips) → retrieval-v1 → RAG DEV regression →
16-case agent regression → 10-case security regression → no-cache image → remove only the
clean-room project's disposable volume → new empty volume DB/migrate/API → HTTP RAG/tenant
and approval/execution/terminal agent → non-root/read-only/caps/tmpfs proof → image gate and
snapshot secret scan. These execute every quality/runtime stage of verify.sh individually,
with the stronger no-cache build and a separate clean-room image tag; verify.sh itself was
not invoked as one command.

[Execution records](execution-records.json), retained logs and the `clean-*.json` reports
establish the results. [Source SHA-256 manifest](validated-source-manifest.json) binds 105
non-document source/config/dataset files to the original tree. Final result documentation and
generated reports were updated afterward; executable inputs were not changed. The sole scanner
policy update (exact public image-signing fingerprint) was copied into the snapshot, its
manifest updated, and Gitleaks rescanned both trees with a passing positive control.

[Clean image/runtime](clean-room-container.json) records an independent local image digest.
No byte-identical build claim is made. Fresh app dependencies and DB state were required;
Docker base images and the externally provisioned Python/uv binaries were reused as toolchains.
Earlier clean-room attempts documented in revalidation handoffs are historical.
