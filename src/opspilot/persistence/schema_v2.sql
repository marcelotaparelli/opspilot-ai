CREATE TABLE agent_runs (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    requested_by text NOT NULL CHECK (length(requested_by) BETWEEN 1 AND 64),
    request text NOT NULL CHECK (length(request) BETWEEN 1 AND 2000),
    status text NOT NULL CHECK (status IN ('planning', 'awaiting_approval', 'approved',
        'executing', 'ambiguous', 'succeeded', 'answered', 'rejected', 'failed')),
    steps integer NOT NULL DEFAULT 0 CHECK (steps >= 0),
    state jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id)
);

-- Immutable once written: the runtime role has no UPDATE on proposals or approvals.
CREATE TABLE agent_proposals (
    run_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    action jsonb NOT NULL,
    action_hash text NOT NULL CHECK (action_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, run_id),
    FOREIGN KEY (tenant_id, run_id) REFERENCES agent_runs (tenant_id, id)
);

CREATE TABLE agent_approvals (
    run_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    decision text NOT NULL CHECK (decision IN ('approved', 'rejected')),
    action_hash text NOT NULL CHECK (action_hash ~ '^[0-9a-f]{64}$'),
    decided_by text NOT NULL CHECK (length(decided_by) BETWEEN 1 AND 64),
    decided_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, run_id),
    FOREIGN KEY (tenant_id, run_id) REFERENCES agent_proposals (tenant_id, run_id)
);

CREATE TABLE agent_executions (
    run_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    action_hash text NOT NULL CHECK (action_hash ~ '^[0-9a-f]{64}$'),
    idempotency_key text NOT NULL UNIQUE,
    status text NOT NULL CHECK (status IN ('pending', 'executing', 'succeeded', 'ambiguous',
        'failed_terminal')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    owner uuid,
    lease_expires_at timestamptz,
    last_attempt_at timestamptz,
    external_issue_id bigint,
    external_issue_iid bigint,
    external_url text CHECK (length(external_url) <= 500),
    last_error text CHECK (last_error ~ '^[a-z0-9_]{1,64}$'),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, run_id) REFERENCES agent_approvals (tenant_id, run_id),
    CHECK (status <> 'executing' OR (owner IS NOT NULL AND lease_expires_at IS NOT NULL))
);

-- Append-only audit trail (no UPDATE/DELETE grants).
CREATE TABLE agent_events (
    id bigserial PRIMARY KEY,
    run_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    at timestamptz NOT NULL DEFAULT clock_timestamp(),
    type text NOT NULL CHECK (type ~ '^[a-z_.]{1,64}$'),
    actor text NOT NULL CHECK (length(actor) BETWEEN 1 AND 64),
    data jsonb NOT NULL DEFAULT '{}'::jsonb,
    FOREIGN KEY (tenant_id, run_id) REFERENCES agent_runs (tenant_id, id)
);

CREATE INDEX agent_runs_tenant ON agent_runs (tenant_id, created_at);
CREATE INDEX agent_events_run ON agent_events (tenant_id, run_id, id);

ALTER TABLE agent_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE agent_proposals ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_proposals FORCE ROW LEVEL SECURITY;
ALTER TABLE agent_approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_approvals FORCE ROW LEVEL SECURITY;
ALTER TABLE agent_executions ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_executions FORCE ROW LEVEL SECURITY;
ALTER TABLE agent_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_events FORCE ROW LEVEL SECURITY;

CREATE POLICY agent_runs_tenant ON agent_runs
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
CREATE POLICY agent_proposals_tenant ON agent_proposals
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
CREATE POLICY agent_approvals_tenant ON agent_approvals
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
CREATE POLICY agent_executions_tenant ON agent_executions
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
CREATE POLICY agent_events_tenant ON agent_events
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON agent_runs, agent_executions TO opspilot_app;
GRANT SELECT, INSERT ON agent_proposals, agent_approvals, agent_events TO opspilot_app;
GRANT USAGE ON SEQUENCE agent_events_id_seq TO opspilot_app;
