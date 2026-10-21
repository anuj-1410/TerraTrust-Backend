-- Generated from BSDD 16.0; apply only through the migration runner.

CREATE TABLE institutions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    profile JSONB NOT NULL DEFAULT '{}'::jsonb,
    consolidation_approach TEXT NOT NULL DEFAULT 'OPERATIONAL_CONTROL',
    verification_status TEXT NOT NULL DEFAULT 'PENDING' CHECK(verification_status IN ('PENDING','APPROVED','REJECTED','SUSPENDED')),
    created_by UUID NOT NULL,
    current_registration_id UUID,
    reviewed_by UUID,
    reviewed_at TIMESTAMPTZ,
    review_reason TEXT,
    archived_at TIMESTAMPTZ,
    UNIQUE(id,created_at)
);
CREATE INDEX institutions_created_idx ON institutions(created_at DESC,id DESC);

CREATE TABLE institution_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    user_id UUID NOT NULL,
    permission TEXT NOT NULL CHECK(permission IN ('OWNER','MANAGER','VIEWER')),
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','ACTIVE','REVOKED')),
    added_by UUID NOT NULL,
    joined_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    UNIQUE(id,institution_id)
);
CREATE INDEX institution_users_created_idx ON institution_users(created_at DESC,id DESC);
CREATE INDEX institution_users_tenant_idx ON institution_users(institution_id);

CREATE TABLE institution_invitations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    target_phone_e164 TEXT NOT NULL,
    permission TEXT NOT NULL CHECK(permission IN ('OWNER','MANAGER','VIEWER')),
    created_by UUID NOT NULL,
    token_hash TEXT NOT NULL CHECK(token_hash ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','ACCEPTED','REVOKED','EXPIRED')),
    expires_at TIMESTAMPTZ NOT NULL,
    accepted_by UUID,
    accepted_at TIMESTAMPTZ,
    supersedes_id UUID,
    UNIQUE(id,institution_id)
);
CREATE INDEX institution_invitations_created_idx ON institution_invitations(created_at DESC,id DESC);
CREATE INDEX institution_invitations_tenant_idx ON institution_invitations(institution_id);

CREATE TABLE institution_registration_authorizations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID,
    target_user_id UUID NOT NULL,
    granted_by UUID NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    reason TEXT NOT NULL,
    UNIQUE(id,institution_id)
);
CREATE INDEX institution_registration_authorizations_created_idx ON institution_registration_authorizations(created_at DESC,id DESC);
CREATE INDEX institution_registration_authorizations_tenant_idx ON institution_registration_authorizations(institution_id);

CREATE TABLE institution_registrations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    logical_registration_id UUID NOT NULL,
    revision INTEGER NOT NULL,
    created_by UUID NOT NULL,
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK(status IN ('DRAFT','SUBMITTED','UNDER_REVIEW','NEEDS_CORRECTION','APPROVED','REJECTED')),
    profile_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence_ids UUID[] NOT NULL DEFAULT '{}',
    input_hash TEXT CHECK(input_hash ~ '^[0-9a-f]{64}$'),
    frozen_input_hash TEXT CHECK(frozen_input_hash ~ '^[0-9a-f]{64}$'),
    supersedes_id UUID,
    submitted_by UUID,
    reviewed_by UUID,
    submitted_at TIMESTAMPTZ,
    reviewed_at TIMESTAMPTZ,
    review_reason TEXT,
    UNIQUE(id,institution_id)
);
CREATE INDEX institution_registrations_created_idx ON institution_registrations(created_at DESC,id DESC);
CREATE INDEX institution_registrations_tenant_idx ON institution_registrations(institution_id);
