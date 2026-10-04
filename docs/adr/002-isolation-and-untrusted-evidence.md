# ADR 002: Tenant scope outside the model, RLS and evidence-bound citations

Status: accepted for Phase 1; adversarial, RLS and pool-reuse checks executed against
PostgreSQL on 2026-10-04 (see [validation record](../VALIDATION.md)).

Map environment-managed bearer credentials to tenants in the HTTP adapter. Client body
tenant IDs are rejected. Every repository transaction uses local tenant configuration;
both SQL filters and forced RLS apply to documents/chunks before retrieval limits. Runtime
role privileges cannot disable RLS, and readiness rejects bypass roles. RLS adds operational
setup and role-management requirements but makes an omitted SQL predicate fail closed.
The policy keys on a GUC the runtime role sets itself, so it does not defend against
arbitrary SQL executed as that role; it defends against omitted predicates and pool leakage.

A wrong-tenant result from any repository implementation fails before context construction.
The LLM receives no tools, credentials or authority over tenant configuration. Retrieved
text sits in a separate untrusted evidence payload. Provider structured output is checked
again by the application against authorized IDs. Citation metadata comes from the database.

Static tokens are deliberately limited: there is no identity federation, fine-grained
document ACL or membership lifecycle. Citation membership does not prove entailment or
prevent malicious evidence from poisoning same-tenant answers. Phase 2 must preserve
external authorization when tools are introduced, rather than granting model-decided access.
