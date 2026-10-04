# Superseded records

Kept unmodified (apart from a superseded banner on the Markdown files) for audit history.
None of these files describes the current state; see [VALIDATION.md](../VALIDATION.md).

| File | What it was | Why superseded |
| --- | --- | --- |
| `codex-blocked-CHECKS.md`, `codex-blocked-validation.json` | First Codex session: every runtime gate blocked (no Python/uv/Docker/Git, sandboxed network) | All gates were later executed |
| `HANDOFF-CODEX-TO-CLAUDE.md`, `CLAUDE-RESUME-PROMPT.md` | Codex handoff claiming gates had passed in a later session | Its evidence under `/tmp/opspilot-validation/` and the Docker tmpfs did not survive VM migration, so nothing in it could be verified; every gate was re-executed from scratch on 2026-10-04 and several of its claims needed correction (see VALIDATION.md, "Divergences from the Codex handoff") |
