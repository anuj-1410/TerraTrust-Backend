-- Generated from BSDD 16.0; apply only through the migration runner.

CREATE TABLE campuses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    name TEXT NOT NULL,
    address JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by UUID NOT NULL,
    control_status TEXT NOT NULL DEFAULT 'PENDING' CHECK(control_status IN ('PENDING','APPROVED','REJECTED','ARCHIVED')),
    control_evidence_ids UUID[] NOT NULL DEFAULT '{}',
    control_input_hash TEXT NOT NULL CHECK(control_input_hash ~ '^[0-9a-f]{64}$'),
    control_approved_hash TEXT CHECK(control_approved_hash ~ '^[0-9a-f]{64}$'),
    control_review_case_id UUID,
    current_boundary_id UUID,
    current_mask_id UUID,
    current_plot_set_id UUID,
    archived_at TIMESTAMPTZ,
    UNIQUE(id,institution_id)
);
CREATE INDEX campuses_created_idx ON campuses(created_at DESC,id DESC);
CREATE INDEX campuses_tenant_idx ON campuses(institution_id);

CREATE TABLE campus_boundaries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    campus_id UUID NOT NULL,
    revision INTEGER NOT NULL,
    geometry geometry(MultiPolygon,4326) NOT NULL CHECK(NOT ST_IsEmpty(geometry) AND ST_IsValid(geometry)),
    geometry_hash TEXT NOT NULL CHECK(geometry_hash ~ '^[0-9a-f]{64}$'),
    area_ha NUMERIC(24,12) NOT NULL,
    effective_start DATE NOT NULL,
    effective_end DATE,
    source TEXT NOT NULL,
    evidence_ids UUID[] NOT NULL DEFAULT '{}',
    created_by UUID NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'PENDING_REVIEW' CHECK(review_status IN ('PENDING_REVIEW','APPROVED','NEEDS_CORRECTION')),
    reviewed_by UUID,
    reviewed_at TIMESTAMPTZ,
    review_reason TEXT,
    input_hash TEXT NOT NULL CHECK(input_hash ~ '^[0-9a-f]{64}$'),
    correction_of_boundary_id UUID,
    UNIQUE(id,institution_id)
);
CREATE INDEX campus_boundaries_created_idx ON campus_boundaries(created_at DESC,id DESC);
CREATE INDEX campus_boundaries_tenant_idx ON campus_boundaries(institution_id);
CREATE INDEX campus_boundaries_geometry_gist ON campus_boundaries USING gist(geometry);

CREATE TABLE campus_vegetation_masks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    campus_id UUID NOT NULL,
    boundary_id UUID NOT NULL,
    method_version UUID NOT NULL,
    revision INTEGER NOT NULL,
    created_by UUID NOT NULL,
    observation_start DATE NOT NULL,
    observation_end DATE NOT NULL,
    eligible_geometry geometry(MultiPolygon,4326) CHECK(NOT ST_IsEmpty(eligible_geometry) AND ST_IsValid(eligible_geometry)),
    eligible_area_ha NUMERIC(24,12),
    map_artifact_id UUID,
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    coverage JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence_ids UUID[] NOT NULL DEFAULT '{}',
    input_hash TEXT NOT NULL CHECK(input_hash ~ '^[0-9a-f]{64}$'),
    processing_status TEXT NOT NULL CHECK(processing_status IN ('IDLE','QUEUED','RUNNING','SUCCEEDED','FAILED','NEEDS_DATA','BLOCKED_ACCESS','CANCELLED')),
    review_status TEXT NOT NULL CHECK(review_status IN ('PENDING_REVIEW','APPROVED','NEEDS_CORRECTION')),
    correction_of_mask_id UUID,
    reviewed_by UUID,
    reviewed_at TIMESTAMPTZ,
    review_reason TEXT,
    UNIQUE(id,institution_id)
);
CREATE INDEX campus_vegetation_masks_created_idx ON campus_vegetation_masks(created_at DESC,id DESC);
CREATE INDEX campus_vegetation_masks_tenant_idx ON campus_vegetation_masks(institution_id);
CREATE INDEX campus_vegetation_masks_eligible_geometry_gist ON campus_vegetation_masks USING gist(eligible_geometry);

CREATE TABLE campus_plot_sets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    campus_id UUID NOT NULL,
    boundary_id UUID NOT NULL,
    mask_id UUID NOT NULL,
    quality_policy_version UUID NOT NULL,
    revision INTEGER NOT NULL,
    created_by UUID NOT NULL,
    design JSONB NOT NULL DEFAULT '{}'::jsonb,
    review_status TEXT NOT NULL CHECK(review_status IN ('PENDING_REVIEW','APPROVED','NEEDS_CORRECTION')),
    input_hash TEXT NOT NULL CHECK(input_hash ~ '^[0-9a-f]{64}$'),
    generation_job_id UUID NOT NULL,
    correction_of_plot_set_id UUID,
    reviewed_by UUID,
    reviewed_at TIMESTAMPTZ,
    review_reason TEXT,
    UNIQUE(id,institution_id)
);
CREATE INDEX campus_plot_sets_created_idx ON campus_plot_sets(created_at DESC,id DESC);
CREATE INDEX campus_plot_sets_tenant_idx ON campus_plot_sets(institution_id);

CREATE TABLE campus_sampling_plots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    campus_id UUID NOT NULL,
    plot_set_id UUID NOT NULL,
    boundary_id UUID NOT NULL,
    mask_id UUID NOT NULL,
    quality_policy_version UUID NOT NULL,
    geometry geometry(MultiPolygon,4326) NOT NULL CHECK(NOT ST_IsEmpty(geometry) AND ST_IsValid(geometry)),
    measured_area_ha NUMERIC(24,12) NOT NULL,
    stratum TEXT NOT NULL,
    inclusion_rule JSONB NOT NULL DEFAULT '{}'::jsonb,
    active_start DATE NOT NULL,
    active_end DATE,
    review_status TEXT NOT NULL CHECK(review_status IN ('PENDING_REVIEW','APPROVED','NEEDS_CORRECTION')),
    UNIQUE(id,institution_id)
);
CREATE INDEX campus_sampling_plots_created_idx ON campus_sampling_plots(created_at DESC,id DESC);
CREATE INDEX campus_sampling_plots_tenant_idx ON campus_sampling_plots(institution_id);
CREATE INDEX campus_sampling_plots_geometry_gist ON campus_sampling_plots USING gist(geometry);

CREATE TABLE campus_trees (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    campus_id UUID NOT NULL,
    stable_tag TEXT NOT NULL,
    location geometry(Point,4326) NOT NULL CHECK(NOT ST_IsEmpty(location) AND ST_IsValid(location)),
    species_code TEXT,
    condition TEXT NOT NULL,
    planting_date DATE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by UUID NOT NULL,
    latest_measurement_id UUID,
    UNIQUE(id,institution_id)
);
CREATE INDEX campus_trees_created_idx ON campus_trees(created_at DESC,id DESC);
CREATE INDEX campus_trees_tenant_idx ON campus_trees(institution_id);
CREATE INDEX campus_trees_location_gist ON campus_trees USING gist(location);

CREATE TABLE campus_tree_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    campus_id UUID NOT NULL,
    tree_id UUID NOT NULL,
    event_type TEXT NOT NULL,
    event_date DATE NOT NULL,
    previous_values JSONB NOT NULL DEFAULT '{}'::jsonb,
    new_values JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    reason TEXT NOT NULL,
    evidence_ids UUID[] NOT NULL DEFAULT '{}',
    actor_id UUID NOT NULL,
    UNIQUE(id,institution_id)
);
CREATE INDEX campus_tree_events_created_idx ON campus_tree_events(created_at DESC,id DESC);
CREATE INDEX campus_tree_events_tenant_idx ON campus_tree_events(institution_id);

CREATE TABLE campus_tree_surveys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    campus_id UUID NOT NULL,
    logical_survey_id UUID NOT NULL,
    boundary_id UUID NOT NULL,
    mask_id UUID NOT NULL,
    plot_set_id UUID NOT NULL,
    quality_policy_version UUID NOT NULL,
    revision INTEGER NOT NULL,
    survey_start DATE NOT NULL,
    survey_end DATE NOT NULL,
    created_by UUID NOT NULL,
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK(status IN ('DRAFT','SUBMITTED','UNDER_REVIEW','ACCEPTED','NEEDS_CORRECTION')),
    input_hash TEXT NOT NULL CHECK(input_hash ~ '^[0-9a-f]{64}$'),
    frozen_input_hash TEXT CHECK(frozen_input_hash ~ '^[0-9a-f]{64}$'),
    supersedes_id UUID,
    submitted_by UUID,
    reviewed_by UUID,
    submitted_at TIMESTAMPTZ,
    reviewed_at TIMESTAMPTZ,
    review_reason TEXT,
    sync_validation JSONB NOT NULL DEFAULT '{}'::jsonb,
    frozen_snapshot JSONB DEFAULT '{}'::jsonb,
    UNIQUE(id,institution_id)
);
CREATE INDEX campus_tree_surveys_created_idx ON campus_tree_surveys(created_at DESC,id DESC);
CREATE INDEX campus_tree_surveys_tenant_idx ON campus_tree_surveys(institution_id);

CREATE TABLE campus_tree_measurements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    campus_id UUID NOT NULL,
    survey_id UUID NOT NULL,
    tree_id UUID NOT NULL,
    plot_id UUID NOT NULL,
    local_uuid UUID NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    measured_at TIMESTAMPTZ NOT NULL,
    location geometry(Point,4326) CHECK(NOT ST_IsEmpty(location) AND ST_IsValid(location)),
    gps_accuracy_m NUMERIC(24,12),
    dbh_cm NUMERIC(24,12),
    height_m NUMERIC(24,12),
    height_source TEXT NOT NULL,
    species_code TEXT,
    species_selection TEXT NOT NULL,
    species_confidence NUMERIC(24,12),
    ar_tier INTEGER NOT NULL,
    allometry_id UUID,
    wood_density_source_id UUID,
    measurement_height_m NUMERIC(24,12) NOT NULL,
    protocol_version UUID NOT NULL,
    measurement_form TEXT NOT NULL,
    circumference_cm NUMERIC(24,12),
    stems JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_ids UUID[] NOT NULL DEFAULT '{}',
    capture_quality JSONB NOT NULL DEFAULT '{}'::jsonb,
    content_hash TEXT NOT NULL CHECK(content_hash ~ '^[0-9a-f]{64}$'),
    raw_input JSONB DEFAULT '{}'::jsonb,
    derived_dbh_cm NUMERIC(24,12),
    validation_status TEXT NOT NULL,
    validation_issues JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_by UUID NOT NULL,
    copied_from_id UUID,
    deleted_at TIMESTAMPTZ,
    UNIQUE(id,institution_id)
);
CREATE INDEX campus_tree_measurements_created_idx ON campus_tree_measurements(created_at DESC,id DESC);
CREATE INDEX campus_tree_measurements_tenant_idx ON campus_tree_measurements(institution_id);
CREATE INDEX campus_tree_measurements_location_gist ON campus_tree_measurements USING gist(location);

CREATE TABLE campus_tree_audits (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    institution_id UUID NOT NULL,
    campus_id UUID NOT NULL,
    survey_id UUID NOT NULL,
    boundary_id UUID NOT NULL,
    mask_id UUID NOT NULL,
    model_version UUID NOT NULL,
    method_version UUID NOT NULL,
    feature_schema_version UUID NOT NULL,
    revision INTEGER NOT NULL,
    assessment_date DATE NOT NULL,
    observation_start DATE NOT NULL,
    observation_end DATE NOT NULL,
    baseline_audit_id UUID,
    correction_of_tree_audit_id UUID,
    created_by UUID NOT NULL,
    input_hash TEXT NOT NULL CHECK(input_hash ~ '^[0-9a-f]{64}$'),
    result_hash TEXT CHECK(result_hash ~ '^[0-9a-f]{64}$'),
    processing_status TEXT NOT NULL CHECK(processing_status IN ('IDLE','QUEUED','RUNNING','SUCCEEDED','FAILED','NEEDS_DATA','BLOCKED_ACCESS','CANCELLED')),
    review_status TEXT NOT NULL DEFAULT 'PENDING' CHECK(review_status IN ('PENDING','ACCEPTED','NEEDS_CORRECTION')),
    job_id UUID NOT NULL,
    agb_t NUMERIC(24,12),
    stock_co2_t NUMERIC(24,12),
    comparison_start DATE,
    comparison_end DATE,
    comparability_status TEXT NOT NULL,
    attribution_policy_version UUID,
    attribution_reason TEXT,
    attribution_sources JSONB NOT NULL DEFAULT '[]'::jsonb,
    event_ids UUID[] NOT NULL DEFAULT '{}',
    attribution_parameter_ids UUID[] NOT NULL DEFAULT '{}',
    coverage JSONB NOT NULL DEFAULT '{}'::jsonb,
    quality_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
    uncertainty JSONB NOT NULL DEFAULT '{}'::jsonb,
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    map_artifact_id UUID,
    reviewed_by UUID,
    reviewed_at TIMESTAMPTZ,
    review_reason TEXT,
    error JSONB DEFAULT '{}'::jsonb,
    UNIQUE(id,institution_id)
);
CREATE INDEX campus_tree_audits_created_idx ON campus_tree_audits(created_at DESC,id DESC);
CREATE INDEX campus_tree_audits_tenant_idx ON campus_tree_audits(institution_id);
