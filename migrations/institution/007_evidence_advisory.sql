-- Generated from BSDD 16.0; apply only through the migration runner.

CREATE TABLE institution_audit_evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID,
    owner_user_id UUID NOT NULL,
    parent_type TEXT NOT NULL,
    parent_id UUID NOT NULL,
    file_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    private_bucket TEXT NOT NULL,
    object_key TEXT NOT NULL,
    expected_sha256 TEXT NOT NULL CHECK(expected_sha256 ~ '^[0-9a-f]{64}$'),
    byte_hash TEXT CHECK(byte_hash ~ '^[0-9a-f]{64}$'),
    size_bytes BIGINT NOT NULL,
    actual_size_bytes BIGINT,
    state TEXT NOT NULL CHECK(state IN ('PENDING_UPLOAD','UPLOADED','CHECKING','READY','REJECTED','EXPIRED')),
    expires_at TIMESTAMPTZ NOT NULL,
    scan_job_id UUID,
    rejection_reason TEXT,
    copied_from_id UUID,
    publication_state TEXT NOT NULL DEFAULT 'PRIVATE',
    manifest_hash TEXT CHECK(manifest_hash ~ '^[0-9a-f]{64}$'),
    cid TEXT,
    bytes_deleted_at TIMESTAMPTZ,
    purge_policy_version UUID,
    UNIQUE(id,institution_id)
);
CREATE INDEX institution_audit_evidence_created_idx ON institution_audit_evidence(created_at DESC,id DESC);
CREATE INDEX institution_audit_evidence_tenant_idx ON institution_audit_evidence(institution_id);

CREATE TABLE institution_evidence_links (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID,
    evidence_id UUID NOT NULL,
    parent_type TEXT NOT NULL,
    parent_id UUID NOT NULL,
    linked_by UUID NOT NULL,
    copied_from_link_id UUID,
    UNIQUE(id,institution_id)
);
CREATE INDEX institution_evidence_links_created_idx ON institution_evidence_links(created_at DESC,id DESC);
CREATE INDEX institution_evidence_links_tenant_idx ON institution_evidence_links(institution_id);

CREATE TABLE advisory_queries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    user_id UUID NOT NULL,
    audit_id UUID NOT NULL,
    snapshot_id UUID NOT NULL,
    question TEXT NOT NULL,
    constraints JSONB NOT NULL DEFAULT '{}'::jsonb,
    context_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    context_hash TEXT NOT NULL CHECK(context_hash ~ '^[0-9a-f]{64}$'),
    versions JSONB NOT NULL DEFAULT '{}'::jsonb,
    answer JSONB DEFAULT '{}'::jsonb,
    answer_hash TEXT CHECK(answer_hash ~ '^[0-9a-f]{64}$'),
    job_id UUID NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('QUEUED','RUNNING','READY','PARTIAL','FAILED','BLOCKED_ACCESS')),
    publication_state TEXT NOT NULL DEFAULT 'PRIVATE' CHECK(publication_state IN ('PRIVATE','PUBLISHED','WITHDRAWN')),
    published_by UUID,
    published_at TIMESTAMPTZ,
    timings JSONB NOT NULL DEFAULT '{}'::jsonb,
    usage JSONB NOT NULL DEFAULT '{}'::jsonb,
    cost NUMERIC(24,12),
    warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
    UNIQUE(id,institution_id)
);
CREATE INDEX advisory_queries_created_idx ON advisory_queries(created_at DESC,id DESC);
CREATE INDEX advisory_queries_tenant_idx ON advisory_queries(institution_id);

CREATE TABLE advisory_retrievals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    query_id UUID NOT NULL,
    chunk_id UUID NOT NULL,
    corpus_version UUID NOT NULL,
    chunk_version INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    semantic_score NUMERIC(24,12),
    lexical_score NUMERIC(24,12),
    fusion_score NUMERIC(24,12),
    reranker_score NUMERIC(24,12),
    filter_decisions JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE(id,institution_id)
);
CREATE INDEX advisory_retrievals_created_idx ON advisory_retrievals(created_at DESC,id DESC);
CREATE INDEX advisory_retrievals_tenant_idx ON advisory_retrievals(institution_id);

CREATE TABLE advisory_recommendations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    query_id UUID NOT NULL,
    action_id TEXT NOT NULL,
    structured_action JSONB NOT NULL DEFAULT '{}'::jsonb,
    priority_class TEXT NOT NULL,
    priority INTEGER NOT NULL,
    ranking_version TEXT NOT NULL,
    rank_components JSONB DEFAULT '{}'::jsonb,
    tie_break TEXT NOT NULL,
    citation_refs JSONB NOT NULL DEFAULT '{}'::jsonb,
    scenario_id UUID,
    validation_flags JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE(id,institution_id)
);
CREATE INDEX advisory_recommendations_created_idx ON advisory_recommendations(created_at DESC,id DESC);
CREATE INDEX advisory_recommendations_tenant_idx ON advisory_recommendations(institution_id);

CREATE TABLE advisory_scenarios (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    audit_id UUID NOT NULL,
    snapshot_id UUID NOT NULL,
    query_id UUID,
    created_by UUID NOT NULL,
    formula_key TEXT NOT NULL,
    assumptions JSONB NOT NULL DEFAULT '{}'::jsonb,
    factor_snapshots JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    overlap_flags JSONB NOT NULL DEFAULT '{}'::jsonb,
    method_version UUID NOT NULL,
    input_hash TEXT NOT NULL CHECK(input_hash ~ '^[0-9a-f]{64}$'),
    hypothetical BOOLEAN NOT NULL DEFAULT true,
    UNIQUE(id,institution_id)
);
CREATE INDEX advisory_scenarios_created_idx ON advisory_scenarios(created_at DESC,id DESC);
CREATE INDEX advisory_scenarios_tenant_idx ON advisory_scenarios(institution_id);

CREATE TABLE advisory_feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    query_id UUID NOT NULL,
    user_id UUID NOT NULL,
    useful BOOLEAN NOT NULL,
    reason TEXT,
    UNIQUE(id,institution_id)
);
CREATE INDEX advisory_feedback_created_idx ON advisory_feedback(created_at DESC,id DESC);
CREATE INDEX advisory_feedback_tenant_idx ON advisory_feedback(institution_id);
