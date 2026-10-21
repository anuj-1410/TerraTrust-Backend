-- Generated from BSDD 16.0; apply only through the migration runner.

CREATE TABLE operator_grants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    user_id UUID NOT NULL,
    capability TEXT NOT NULL,
    institution_id UUID,
    case_id UUID,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ NOT NULL,
    granted_by UUID NOT NULL,
    revoked_at TIMESTAMPTZ,
    UNIQUE(id,institution_id)
);
CREATE INDEX operator_grants_created_idx ON operator_grants(created_at DESC,id DESC);
CREATE INDEX operator_grants_tenant_idx ON operator_grants(institution_id);

CREATE TABLE review_cases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id UUID NOT NULL,
    revision INTEGER NOT NULL,
    resource_input_revision INTEGER NOT NULL,
    input_hash TEXT NOT NULL CHECK(input_hash ~ '^[0-9a-f]{64}$'),
    result_hash TEXT CHECK(result_hash ~ '^[0-9a-f]{64}$'),
    resource_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL,
    assigned_reviewer UUID,
    claim_expires_at TIMESTAMPTZ,
    decision TEXT,
    reason TEXT,
    field_issues JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_ids UUID[] NOT NULL DEFAULT '{}',
    approved_snapshot_id UUID,
    UNIQUE(id,institution_id)
);
CREATE INDEX review_cases_created_idx ON review_cases(created_at DESC,id DESC);
CREATE INDEX review_cases_tenant_idx ON review_cases(institution_id);

CREATE TABLE processing_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID,
    domain TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    resource_id UUID NOT NULL,
    requested_by UUID NOT NULL,
    job_type TEXT NOT NULL,
    source_input_revision INTEGER NOT NULL,
    input_hash TEXT NOT NULL CHECK(input_hash ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL CHECK(status IN ('IDLE','QUEUED','RUNNING','SUCCEEDED','FAILED','NEEDS_DATA','BLOCKED_ACCESS','CANCELLED')),
    stage TEXT NOT NULL,
    progress INTEGER,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    frozen_input JSONB NOT NULL DEFAULT '{}'::jsonb,
    stage_outputs JSONB NOT NULL DEFAULT '{}'::jsonb,
    provider_task_ids JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_ref UUID,
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    deadline_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    retryable BOOLEAN NOT NULL DEFAULT false,
    error JSONB DEFAULT '{}'::jsonb,
    fixture_generation INTEGER,
    UNIQUE(id,institution_id)
);
CREATE INDEX processing_jobs_created_idx ON processing_jobs(created_at DESC,id DESC);
CREATE INDEX processing_jobs_tenant_idx ON processing_jobs(institution_id);

CREATE TABLE job_outbox (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    job_id UUID NOT NULL,
    payload_hash TEXT NOT NULL CHECK(payload_hash ~ '^[0-9a-f]{64}$'),
    queue_name TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ,
    dispatched_at TIMESTAMPTZ,
    UNIQUE(id,created_at)
);
CREATE INDEX job_outbox_created_idx ON job_outbox(created_at DESC,id DESC);

CREATE TABLE audit_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID,
    domain TEXT NOT NULL,
    actor_id UUID,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id UUID NOT NULL,
    old_hash TEXT CHECK(old_hash ~ '^[0-9a-f]{64}$'),
    new_hash TEXT CHECK(new_hash ~ '^[0-9a-f]{64}$'),
    reason TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    event_at TIMESTAMPTZ NOT NULL,
    UNIQUE(id,institution_id)
);
CREATE INDEX audit_events_created_idx ON audit_events(created_at DESC,id DESC);
CREATE INDEX audit_events_tenant_idx ON audit_events(institution_id);

CREATE TABLE idempotency_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID,
    user_id UUID NOT NULL,
    http_method TEXT NOT NULL,
    normalized_route TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'),
    intended_revision INTEGER,
    http_status INTEGER NOT NULL,
    accepted_resource_id UUID,
    response JSONB DEFAULT '{}'::jsonb,
    receipt_state TEXT NOT NULL DEFAULT 'FULL',
    secret_ciphertext BYTEA,
    encryption_key_id TEXT,
    secret_expires_at TIMESTAMPTZ,
    fixture_generation INTEGER,
    expires_at TIMESTAMPTZ NOT NULL,
    accepted_revision INTEGER,
    recovery_kind TEXT NOT NULL,
    UNIQUE(id,institution_id)
);
CREATE INDEX idempotency_records_created_idx ON idempotency_records(created_at DESC,id DESC);
CREATE INDEX idempotency_records_tenant_idx ON idempotency_records(institution_id);

CREATE TABLE institution_notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID,
    recipient_user_id UUID NOT NULL,
    event_id UUID NOT NULL,
    type TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id UUID NOT NULL,
    revision INTEGER,
    safe_summary TEXT NOT NULL,
    read_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    UNIQUE(id,institution_id)
);
CREATE INDEX institution_notifications_created_idx ON institution_notifications(created_at DESC,id DESC);
CREATE INDEX institution_notifications_tenant_idx ON institution_notifications(institution_id);

CREATE TABLE institution_deletion_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    user_id UUID NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    resolution JSONB NOT NULL DEFAULT '{}'::jsonb,
    request_hash TEXT NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'),
    policy_version UUID,
    plan_hash TEXT CHECK(plan_hash ~ '^[0-9a-f]{64}$'),
    verified_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    operator_id UUID,
    job_id UUID,
    UNIQUE(id,created_at)
);
CREATE INDEX institution_deletion_requests_created_idx ON institution_deletion_requests(created_at DESC,id DESC);
