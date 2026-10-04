# ADR 004: LangGraph for agent control flow; PostgreSQL domain records for durability

Status: accepted for Phase 2 (2026-10-04). Evidence: [agent-workflow.md](../architecture/agent-workflow.md).

## Context

Phase 2 adds a bounded agent that may propose a GitLab issue, wait for a human approval bound to
the exact action, and then execute one external side effect. That side effect must survive
restarts, must not be duplicated, and must remain safe when the result is unknown. The owner
asked for LangGraph as the framework, used only where it adds value.

## Decision

Use **LangGraph 1.2.12** for control flow only:

- `StateGraph` over an explicit `TypedDict` state (`AgentState`);
- nodes (`plan`, `search_knowledge`, `prepare_gitlab_issue`, `final_answer`, `execute`);
- conditional routing from `START` and after each node;
- `recursion_limit` / `GraphRecursionError` as a second, independent loop bound behind the
  application's own step counter;
- `get_graph()` so a test can assert the topology: the only edge into `execute` comes from
  `START`, and planning cannot reach execution.

Do **not** use the LangGraph checkpointer, `interrupt()`/`Command`, prebuilt agents/`ToolNode`,
LangChain messages, tools or chains, or LangSmith.

Durability lives in our own PostgreSQL tables (`agent_runs`, `agent_proposals`,
`agent_approvals`, `agent_executions`, `agent_events`), under the same RLS and tenant-transaction
model as Phase 1. Each node persists its transition before returning. Human-in-the-loop is a
durable stop: the graph ends at `awaiting_approval`. An approval transaction later sets the
status, and a new invocation enters at `execute` through the entry router.

## Why not a hand-written loop

A loop would be about the same size today. LangGraph still earns its place:
- the topology is declared, so a test can check the safety-relevant shape (no planning → execute edge);
- the recursion limit gives a framework-level backstop independent of our counter, which the
  mutation tests rely on;
- per-node boundaries map one-to-one onto the Phase 3 tracing spans;
- growing to more tools stays explicit instead of turning into nested conditionals.

## Why not the LangGraph checkpointer + `interrupt()`

- **Two sources of truth.** Approval validity and side-effect ownership must be decided by
  transactional SQL (row locks, unique constraints, owner/lease fencing, RLS). A checkpoint blob
  holding a second copy of status/proposal could disagree with those rows. Deciding which one
  wins is a bug class we avoid entirely.
- **Approval binding.** `interrupt()` resumes a node by replaying it. Our approval must be
  bound to a hash of an immutable stored action and checked again before the side effect,
  whatever the graph state says.
- **Dependencies.** The official Postgres checkpointer brings psycopg and its pool alongside our
  asyncpg/SQLAlchemy stack: a second driver and a second schema to migrate and secure.
- **Audit.** Our tables are queryable, tenant-isolated and append-only where it matters.
  Checkpoint blobs are opaque.

## Consequences

- A crash during planning leaves `status='planning'`. `POST /resume` continues from the last
  persisted node; planning has no side effects, so repeating an LLM step is acceptable.
- A crash during execution is recovered from `agent_executions` (lease expiry → reconcile by
  marker). LangGraph holds nothing that matters across processes.
- Cost: LangGraph pulls in `langchain-core` and `langsmith` (lock grew from 33 to 58 entries, +25,
  see [DEPENDENCIES.md](../DEPENDENCIES.md)). LangSmith tracing stays off unless its environment
  variables are set; none are set by this project.
- If we ever need long-running multi-step human interaction inside a node, revisit
  `interrupt()` with a checkpointer that writes into these same tables.
