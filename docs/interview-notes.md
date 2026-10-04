# Interview notes — decisions and evidence

These are engineering decision records, not a memorized presentation. Refer to the linked
artifacts, distinguish historical measurements from final gates and identify what remains unknown.

## A benchmark that initially told us too little

Retrieval-v1 had near-duplicate query/document wording and a small synthetic corpus. Strong
numbers did not distinguish lexical, fake-vector and hybrid behavior. Retrieval-v2 added 70
documents across two tenants, 24 dev queries and 36 held-out queries, graded relevance and hard
negatives. Fingerprints/freeze checks made the once-consumed held-out result attributable to
its sources/configuration. It is not a test set to rerun after later edits.
[Design and limitations](evaluation/retrieval-v2.md).

## Lexical, vector and hybrid did not rank as expected

With the deterministic hashed bag-of-words embedder, held-out lexical MRR@5 was 0.7532 versus
vector 0.4375 and hybrid 0.6306. RRF recomputation verified actual fusion, so hybrid's weak
result was not merely a disconnected vector branch. Adding a poor ranking can hurt a better
one. The decision was to retain the experiment's limitations, not claim hybrid superiority or
extrapolate to semantic embeddings. No real embedding-quality experiment was performed.

## Two lexical bugs changed the meaning of the results

`simple` has no stopword list; AND semantics made natural-language questions such as “How do
I restart the service?” match nothing. OR word terms fixed recall. Later retrieval-v2 dev exposed
`ts_rank_cd` behaving like occurrence counting under OR, favoring long chunks. A dev-only
comparison of six variants selected `ts_rank`; lexical dev MRR@5 rose from 0.4965 to 0.6958.
The held-out run happened after freeze, without further tuning.

## Isolation was tested at several boundaries

Tokens determine tenant scope outside the model. Both SQL branches filter before ranking and
limits; forced RLS protects against an omitted predicate. Every transaction sets tenant context
locally, so another request reusing the pool does not inherit it. Application context validation
rejects wrong-tenant candidates even from another repository adapter. RLS's GUC can be set by
the runtime role: arbitrary SQL as that role can choose scope, so this is defense in depth,
not protection from a compromised SQL executor. [ADR 002](adr/002-isolation-and-untrusted-evidence.md).

## Human approval bound to bytes, not an LLM assertion

The planner can draft an issue but cannot approve/execute it. Canonical JSON hashing binds
approval to the exact stored action; requester and approver need distinct subjects. Persisted
state/claims prevent concurrent approvals from independently owning execution. Current policy
is checked again just before creating the issue: removing a project after proposal/approval
must prevent execution. This handles the tested TOCTOU cases, but policy changes after the
last check or an already-sent HTTP request remain external timing limits.
[Workflow](architecture/agent-workflow.md).

## “The response failed” did not mean “nothing happened”

Fake GitLab faults drop, delay or corrupt the response after storing the issue. A request might
commit remotely and fail locally. Such outcomes become ambiguous, retain the action marker
and are reconciled before bounded resend. Historical tests kill the process mid-request and
fail the DB write after GitLab success. Marker lookup recovers the issue without immediate
blind retry. Delayed real search visibility, marker edits/deletion and expired in-flight leases
can still produce duplicates; no exactly-once claim is justified. Recovery is invoked via
`/resume`, not a background worker.

Phase 4 continuation found that the live-smoke counter counted received responses instead
of sent attempts, and cleanup relied on receiving an IID. It now records unknown HTTP status
for lost responses and attempts cleanup by the approved action key, verifying project, IID
and the complete description before closing a candidate. Cleanup lookup attempts also appear
in the final report. New fault regressions still need final
execution against real PostgreSQL/fake GitLab; do not present the patch as a validated live result.

## OpenTelemetry fail-open needed an independent privacy boundary

Manual spans describe business steps, tokens and estimated cost, with bounded metric labels.
Unknown usage/cost remains unknown. Slow/dead exporters must not block requests; background
export can lose telemetry. Phase 4 disables new framework-native automatic telemetry to avoid
uncontrolled fields/export. Served model IDs are span-only. JSON HTTP logs now expose status
without echoing exception/request content. [Observability](observability.md).

## A broken mutation baseline produced misleading success

The first Phase 3 mutation attempt started with an already-failing audit-event test after
`trace_id` was added. Every mutant appeared “caught” by the unrelated baseline failure. That
run was discarded. The baseline was fixed and reverified (201 unit / 64 integration), then all
14 mutants were rerun and caught by relevant tests. The lesson is concrete: validate baseline
and failure attribution before reporting mutation coverage.
[Record](evidence/phase3/mutation-results.txt).

## Real providers were separated from CI intentionally

CI measures local contracts and safety with transport mocks, fake planners, fake GitLab and
real PostgreSQL. Those results cannot establish model choice quality or real provider behavior.
Opt-in live scripts require credentials plus an explicit allow flag; missing configuration
must exit 2 without network. OpenAI reserves at most nine calls and checks actual constrained
schemas, including the unresolved minLength/maxLength question. GitLab uses a least-privilege
sandbox token and closes smoke issues. Neither provider was called; both remain
NOT EXECUTED — CREDENTIALS NOT PROVIDED.

The continuation also found that some smoke facts were recorded without influencing PASS.
Evidence predicates now require usage/model/cost when configured, trace IDs, valid evidence
and a classified bounded timeout. Negative tests passed when these observations were deliberately removed.
Passing rehearsals remain separate from unexecuted live proof.

## Release discipline

Phase 4 completed all critical engineering gates and cold clean-room before the sole final
release commit. The CLI logger lifecycle bug was fixed and repeated closed-stream calls are
covered. Engineering PASS includes 241 unit / 72 integration, migration rollback/retry,
regressions, runtime/HTTP, scans and offline Terraform. Unfixed image findings and deliberate
IaC risks remain visible. Live OpenAI/GitLab and AWS execution remain unverified; no provider
credentials were requested, held-out evaluation rerun, infrastructure applied or push made.
