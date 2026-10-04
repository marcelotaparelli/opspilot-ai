# ADR 001: Ports with SQLAlchemy async and transactional SQL migration

Status: accepted for Phase 1; runtime regression checks executed on 2026-10-04
(see [validation record](../VALIDATION.md)).

Use plain dataclasses/Protocols for application boundaries, Pydantic at input/output
adapters, and SQLAlchemy 2 async with asyncpg for pooling and transactions. Parameterized
SQL keeps PostgreSQL vector/FTS/RLS semantics visible without creating an ORM model layer.
The SQLAlchemy dependency adds pooling/lifecycle complexity but avoids maintaining these
facilities in application code. Errors are translated before reaching HTTP/log boundaries.

One packaged SQL migration is versioned in `schema_version`, guarded by a transaction
advisory lock and applied with separate admin credentials. It is atomic and reentrant
when v1 already exists. Avoiding Alembic reduces dependency/configuration overhead now;
multiple migrations, upgrades and deployment concurrency will warrant a migration framework.

The runtime uses a dedicated SELECT/INSERT role; administrators remain trusted. Startup
requires schema/role readiness; it does not perform DDL with application credentials.
