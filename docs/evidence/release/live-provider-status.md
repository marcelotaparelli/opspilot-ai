# Live provider evidence

OPENAI LIVE EVIDENCE: NOT EXECUTED — CREDENTIALS NOT PROVIDED

GITLAB LIVE EVIDENCE: NOT EXECUTED — CREDENTIALS NOT PROVIDED

No paid OpenAI request or real GitLab side effect was performed. Credential values are not
requested, printed or recorded. Scripts require explicit allow flags as well as complete
configuration; missing configuration must exit 2 before network work.

OpenAI's smoke reserves at most nine requests: model retrieval, 256-d embedding, strict answer
and planner schemas, optional DB-backed small RAG and a tight timeout. Evidence predicates
require observed usage, served model IDs, cost when pricing is configured, trace IDs, authorized
evidence and an actual bounded timeout classification. String schema constraints include
minLength/maxLength; live acceptance/rejection is unknown. Local validation does not justify
removing constraints or declaring real API compatibility.

GitLab's smoke requires HTTPS, sandbox project ID, token and migrated database plus allow flag.
It exercises proposal/approval/create/GET, second approval 409, resume without duplicate, then
close. Partial failure cleanup attempts marker lookup if no IID was received. Attempts are
counted even with lost responses; unknown response status remains unknown. Search/cleanup can
fail, so sandbox leftovers still require operator reconciliation.

Rehearsals use mock OpenAI transport and fake GitLab with real PostgreSQL. The continuation
adds negative evidence and lost-response cleanup regressions; final full unit/integration rehearsals and clean-room passed. They remain distinct from live evidence.
