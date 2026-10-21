-- Generated from BSDD 16.0; apply only through the migration runner.

CREATE TABLE institution_audit_periods (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    timezone TEXT NOT NULL,
    label TEXT NOT NULL,
    created_by UUID NOT NULL,
    period_range DATERANGE GENERATED ALWAYS AS (daterange(period_start,period_end+1,'[)')) STORED,
    current_final_audit_id UUID,
    active_draft_audit_id UUID,
    UNIQUE(id,institution_id)
);
CREATE INDEX institution_audit_periods_created_idx ON institution_audit_periods(created_at DESC,id DESC);
CREATE INDEX institution_audit_periods_tenant_idx ON institution_audit_periods(institution_id);

CREATE TABLE institution_audits (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    logical_audit_id UUID NOT NULL,
    revision INTEGER NOT NULL,
    supersedes_id UUID,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    timezone TEXT NOT NULL,
    label TEXT NOT NULL,
    campus_ids UUID[] NOT NULL DEFAULT '{}',
    coverage JSONB NOT NULL DEFAULT '{}'::jsonb,
    denominators JSONB NOT NULL DEFAULT '{}'::jsonb,
    boundary_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    method_version UUID NOT NULL,
    created_by UUID NOT NULL,
    business_status TEXT NOT NULL DEFAULT 'DRAFT' CHECK(business_status IN ('DRAFT','SUBMITTED','UNDER_REVIEW','NEEDS_CORRECTION','FINALIZED','SUPERSEDED')),
    processing_status TEXT NOT NULL CHECK(processing_status IN ('IDLE','QUEUED','RUNNING','SUCCEEDED','FAILED','NEEDS_DATA','BLOCKED_ACCESS','CANCELLED')),
    input_hash TEXT NOT NULL CHECK(input_hash ~ '^[0-9a-f]{64}$'),
    frozen_snapshot JSONB DEFAULT '{}'::jsonb,
    frozen_input_hash TEXT CHECK(frozen_input_hash ~ '^[0-9a-f]{64}$'),
    preview_snapshot_id UUID,
    reviewed_snapshot_id UUID,
    current_result_id UUID,
    submitted_by UUID,
    reviewed_by UUID,
    finalized_by UUID,
    submitted_at TIMESTAMPTZ,
    reviewed_at TIMESTAMPTZ,
    finalized_at TIMESTAMPTZ,
    latest_review_reason TEXT,
    UNIQUE(id,institution_id)
);
CREATE INDEX institution_audits_created_idx ON institution_audits(created_at DESC,id DESC);
CREATE INDEX institution_audits_tenant_idx ON institution_audits(institution_id);

CREATE TABLE institution_emission_inputs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    audit_id UUID NOT NULL,
    campus_id UUID,
    category TEXT NOT NULL,
    subcategory TEXT NOT NULL,
    source_id TEXT NOT NULL,
    scope TEXT,
    quantity NUMERIC(24,12),
    unit TEXT NOT NULL,
    activity_start DATE NOT NULL,
    activity_end DATE NOT NULL,
    source_type TEXT NOT NULL CHECK(source_type IN ('MEASURED','REPORTED','ESTIMATED')),
    estimate_notes TEXT,
    evidence_ids UUID[] NOT NULL DEFAULT '{}',
    included_in_input_id UUID,
    allocation_ratio NUMERIC(24,12),
    factor_id UUID,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    flags JSONB NOT NULL DEFAULT '[]'::jsonb,
    copied_from_id UUID,
    created_by UUID NOT NULL,
    UNIQUE(id,institution_id)
);
CREATE INDEX institution_emission_inputs_created_idx ON institution_emission_inputs(created_at DESC,id DESC);
CREATE INDEX institution_emission_inputs_tenant_idx ON institution_emission_inputs(institution_id);

CREATE TABLE institution_mitigation_inputs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    audit_id UUID NOT NULL,
    campus_id UUID,
    action TEXT NOT NULL,
    activity_start DATE NOT NULL,
    activity_end DATE NOT NULL,
    method_version UUID NOT NULL,
    source_type TEXT NOT NULL CHECK(source_type IN ('MEASURED','REPORTED','ESTIMATED')),
    estimate_notes TEXT,
    baseline JSONB NOT NULL DEFAULT '{}'::jsonb,
    project JSONB NOT NULL DEFAULT '{}'::jsonb,
    attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
    overlap_refs UUID[] NOT NULL DEFAULT '{}',
    evidence_ids UUID[] NOT NULL DEFAULT '{}',
    flags JSONB NOT NULL DEFAULT '[]'::jsonb,
    copied_from_id UUID,
    created_by UUID NOT NULL,
    UNIQUE(id,institution_id)
);
CREATE INDEX institution_mitigation_inputs_created_idx ON institution_mitigation_inputs(created_at DESC,id DESC);
CREATE INDEX institution_mitigation_inputs_tenant_idx ON institution_mitigation_inputs(institution_id);

CREATE TABLE institution_audit_tree_links (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    audit_id UUID NOT NULL,
    campus_id UUID NOT NULL,
    campus_tree_audit_id UUID NOT NULL,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    comparability_status TEXT NOT NULL,
    reason TEXT,
    attribution_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by UUID NOT NULL,
    UNIQUE(id,institution_id)
);
CREATE INDEX institution_audit_tree_links_created_idx ON institution_audit_tree_links(created_at DESC,id DESC);
CREATE INDEX institution_audit_tree_links_tenant_idx ON institution_audit_tree_links(institution_id);

CREATE TABLE institution_calculation_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    audit_id UUID NOT NULL,
    source_input_revision INTEGER NOT NULL,
    input_hash TEXT NOT NULL CHECK(input_hash ~ '^[0-9a-f]{64}$'),
    result_hash TEXT NOT NULL CHECK(result_hash ~ '^[0-9a-f]{64}$'),
    processing_job_id UUID NOT NULL,
    frozen_input JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_as_preview BOOLEAN NOT NULL,
    quality JSONB NOT NULL DEFAULT '{}'::jsonb,
    missing_data JSONB NOT NULL DEFAULT '[]'::jsonb,
    UNIQUE(id,institution_id)
);
CREATE INDEX institution_calculation_snapshots_created_idx ON institution_calculation_snapshots(created_at DESC,id DESC);
CREATE INDEX institution_calculation_snapshots_tenant_idx ON institution_calculation_snapshots(institution_id);

CREATE TABLE institution_calculation_lines (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    audit_id UUID NOT NULL,
    snapshot_id UUID NOT NULL,
    input_type TEXT NOT NULL,
    input_id UUID,
    formula_key TEXT NOT NULL,
    method_version UUID NOT NULL,
    normalized_quantity NUMERIC(24,12),
    normalized_unit TEXT,
    factor_id UUID,
    factor_snapshots JSONB NOT NULL DEFAULT '{}'::jsonb,
    baseline_terms JSONB DEFAULT '{}'::jsonb,
    project_terms JSONB DEFAULT '{}'::jsonb,
    contribution_amount NUMERIC(24,12),
    result_unit TEXT NOT NULL,
    scope TEXT,
    included_contribution BOOLEAN NOT NULL,
    linked_parent_id UUID,
    assumptions JSONB NOT NULL DEFAULT '{}'::jsonb,
    flags JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_ids UUID[] NOT NULL DEFAULT '{}',
    UNIQUE(id,institution_id)
);
CREATE INDEX institution_calculation_lines_created_idx ON institution_calculation_lines(created_at DESC,id DESC);
CREATE INDEX institution_calculation_lines_tenant_idx ON institution_calculation_lines(institution_id);

CREATE TABLE institution_audit_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    audit_id UUID NOT NULL,
    snapshot_id UUID NOT NULL,
    result_revision INTEGER NOT NULL,
    input_hash TEXT NOT NULL CHECK(input_hash ~ '^[0-9a-f]{64}$'),
    result_hash TEXT NOT NULL CHECK(result_hash ~ '^[0-9a-f]{64}$'),
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE(id,institution_id)
);
CREATE INDEX institution_audit_results_created_idx ON institution_audit_results(created_at DESC,id DESC);
CREATE INDEX institution_audit_results_tenant_idx ON institution_audit_results(institution_id);

CREATE TABLE institution_import_batches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    audit_id UUID NOT NULL,
    evidence_id UUID NOT NULL,
    source_input_revision INTEGER NOT NULL,
    file_hash TEXT NOT NULL CHECK(file_hash ~ '^[0-9a-f]{64}$'),
    normalized_hash TEXT NOT NULL CHECK(normalized_hash ~ '^[0-9a-f]{64}$'),
    column_mapping JSONB NOT NULL DEFAULT '{}'::jsonb,
    schema_version INTEGER NOT NULL DEFAULT 1,
    operation TEXT NOT NULL DEFAULT 'APPEND_NEW',
    status TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_by UUID NOT NULL,
    valid_count INTEGER NOT NULL,
    error_count INTEGER NOT NULL,
    duplicate_count INTEGER NOT NULL,
    committed_input_revision INTEGER,
    committed_by UUID,
    committed_at TIMESTAMPTZ,
    UNIQUE(id,institution_id)
);
CREATE INDEX institution_import_batches_created_idx ON institution_import_batches(created_at DESC,id DESC);
CREATE INDEX institution_import_batches_tenant_idx ON institution_import_batches(institution_id);

CREATE TABLE institution_import_rows (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    audit_id UUID NOT NULL,
    batch_id UUID NOT NULL,
    line_id UUID NOT NULL,
    row_number INTEGER NOT NULL,
    normalized_line JSONB NOT NULL DEFAULT '{}'::jsonb,
    errors JSONB NOT NULL DEFAULT '{}'::jsonb,
    warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
    UNIQUE(id,institution_id)
);
CREATE INDEX institution_import_rows_created_idx ON institution_import_rows(created_at DESC,id DESC);
CREATE INDEX institution_import_rows_tenant_idx ON institution_import_rows(institution_id);

CREATE TABLE institution_exports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    audit_id UUID NOT NULL,
    snapshot_id UUID NOT NULL,
    result_hash TEXT NOT NULL CHECK(result_hash ~ '^[0-9a-f]{64}$'),
    format TEXT NOT NULL CHECK(format IN ('PDF','CSV','JSON')),
    job_id UUID NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('IDLE','QUEUED','RUNNING','SUCCEEDED','FAILED','NEEDS_DATA','BLOCKED_ACCESS','CANCELLED')),
    artifact_id UUID,
    draft_watermark BOOLEAN NOT NULL,
    include_advice BOOLEAN NOT NULL,
    advice_query_ids UUID[] NOT NULL DEFAULT '{}',
    advice_hashes JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_by UUID NOT NULL,
    UNIQUE(id,institution_id)
);
CREATE INDEX institution_exports_created_idx ON institution_exports(created_at DESC,id DESC);
CREATE INDEX institution_exports_tenant_idx ON institution_exports(institution_id);

CREATE TABLE institution_artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    parent_type TEXT NOT NULL,
    parent_id UUID NOT NULL,
    producer_job_id UUID,
    snapshot_id UUID,
    private_bucket TEXT NOT NULL,
    object_key TEXT NOT NULL,
    byte_hash TEXT CHECK(byte_hash ~ '^[0-9a-f]{64}$'),
    size_bytes BIGINT,
    mime_type TEXT NOT NULL,
    status TEXT NOT NULL,
    draft BOOLEAN NOT NULL,
    bytes_deleted_at TIMESTAMPTZ,
    purge_policy_version UUID,
    UNIQUE(id,institution_id)
);
CREATE INDEX institution_artifacts_created_idx ON institution_artifacts(created_at DESC,id DESC);
CREATE INDEX institution_artifacts_tenant_idx ON institution_artifacts(institution_id);
