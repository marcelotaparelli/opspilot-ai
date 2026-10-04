CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE documents (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    source text CHECK (length(source) <= 500),
    tags jsonb NOT NULL DEFAULT '[]'::jsonb,
    content text NOT NULL CHECK (length(content) BETWEEN 1 AND 100000),
    embedding_space text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id)
);

CREATE TABLE chunks (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    document_id uuid NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    content text NOT NULL CHECK (length(content) BETWEEN 1 AND 1200),
    start_offset integer NOT NULL CHECK (start_offset >= 0),
    end_offset integer NOT NULL CHECK (end_offset > start_offset),
    embedding vector(256) NOT NULL,
    embedding_space text NOT NULL,
    search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    FOREIGN KEY (tenant_id, document_id) REFERENCES documents(tenant_id, id) ON DELETE CASCADE,
    UNIQUE (document_id, ordinal),
    CHECK (end_offset - start_offset = length(content))
);

CREATE INDEX documents_tenant ON documents (tenant_id);
CREATE INDEX chunks_tenant_space ON chunks (tenant_id, embedding_space);
CREATE INDEX chunks_lexical ON chunks USING gin (search_vector);

ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks FORCE ROW LEVEL SECURITY;

CREATE POLICY documents_tenant ON documents
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
CREATE POLICY chunks_tenant ON chunks
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT ON documents, chunks TO opspilot_app;
GRANT SELECT ON schema_version TO opspilot_app;
