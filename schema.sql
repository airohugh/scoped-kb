-- scoped-kb schema. Requires the pgvector extension.
-- Tested with pgvector 0.8.x on PostgreSQL 17.
--
-- There is no ownership column: which collection a row belongs to is decided by the
-- registry. There is deliberately no vector index either (a full scan is fine at a few
-- thousand rows; add HNSW yourself past ~10k chunks).

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS knowledge_index (
    id               bigserial PRIMARY KEY,
    ts               timestamptz NOT NULL DEFAULT now(),
    source_agent     varchar(50),
    file_path        text NOT NULL,
    content          text NOT NULL,
    content_hash     char(64) NOT NULL,
    trust_tier       char(2) CHECK (trust_tier IN ('T1', 'T2', 'T3')),
    embedding        vector(1024),
    embedding_model  varchar(50),
    collection_id    text NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_kidx_chunk ON knowledge_index (file_path, content_hash);
CREATE INDEX IF NOT EXISTS idx_kidx_path ON knowledge_index (file_path);
CREATE INDEX IF NOT EXISTS idx_knowledge_index_collection ON knowledge_index (collection_id);
CREATE INDEX IF NOT EXISTS idx_kidx_pending ON knowledge_index (ts) WHERE embedding IS NULL;

-- Single-row table recording which registry generation the index was built from.
CREATE TABLE IF NOT EXISTS knowledge_index_meta (
    id                    boolean PRIMARY KEY DEFAULT true CHECK (id),
    active_registry_hash  text NOT NULL,
    indexed_at            timestamptz NOT NULL DEFAULT now()
);
