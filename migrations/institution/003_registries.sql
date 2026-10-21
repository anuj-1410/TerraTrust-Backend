-- Generated from BSDD 16.0; apply only through the migration runner.

CREATE TABLE emission_factors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    logical_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL,
    publisher TEXT NOT NULL,
    source_version TEXT NOT NULL,
    license TEXT NOT NULL,
    source_url TEXT,
    source_page TEXT,
    source_section TEXT,
    source_evidence_id UUID,
    published_date DATE,
    valid_from DATE,
    valid_to DATE,
    geography_codes TEXT[] NOT NULL DEFAULT '{}',
    applicability_notes TEXT NOT NULL,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK(status IN ('DRAFT','APPROVED','ACTIVE','DEACTIVATED','SUPERSEDED')),
    artifact_hash TEXT NOT NULL CHECK(artifact_hash ~ '^[0-9a-f]{64}$'),
    author_id UUID NOT NULL,
    reviewer_id UUID,
    validated_at TIMESTAMPTZ,
    supersedes_id UUID,
    UNIQUE(id,created_at)
);
CREATE INDEX emission_factors_created_idx ON emission_factors(created_at DESC,id DESC);

CREATE TABLE carbon_methodologies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    logical_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL,
    publisher TEXT NOT NULL,
    source_version TEXT NOT NULL,
    license TEXT NOT NULL,
    source_url TEXT,
    source_page TEXT,
    source_section TEXT,
    source_evidence_id UUID,
    published_date DATE,
    valid_from DATE,
    valid_to DATE,
    geography_codes TEXT[] NOT NULL DEFAULT '{}',
    applicability_notes TEXT NOT NULL,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK(status IN ('DRAFT','APPROVED','ACTIVE','DEACTIVATED','SUPERSEDED')),
    artifact_hash TEXT NOT NULL CHECK(artifact_hash ~ '^[0-9a-f]{64}$'),
    author_id UUID NOT NULL,
    reviewer_id UUID,
    validated_at TIMESTAMPTZ,
    supersedes_id UUID,
    UNIQUE(id,created_at)
);
CREATE INDEX carbon_methodologies_created_idx ON carbon_methodologies(created_at DESC,id DESC);

CREATE TABLE model_registry (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    logical_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL,
    publisher TEXT NOT NULL,
    source_version TEXT NOT NULL,
    license TEXT NOT NULL,
    source_url TEXT,
    source_page TEXT,
    source_section TEXT,
    source_evidence_id UUID,
    published_date DATE,
    valid_from DATE,
    valid_to DATE,
    geography_codes TEXT[] NOT NULL DEFAULT '{}',
    applicability_notes TEXT NOT NULL,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK(status IN ('DRAFT','APPROVED','ACTIVE','DEACTIVATED','SUPERSEDED')),
    artifact_hash TEXT NOT NULL CHECK(artifact_hash ~ '^[0-9a-f]{64}$'),
    author_id UUID NOT NULL,
    reviewer_id UUID,
    validated_at TIMESTAMPTZ,
    supersedes_id UUID,
    UNIQUE(id,created_at)
);
CREATE INDEX model_registry_created_idx ON model_registry(created_at DESC,id DESC);

CREATE TABLE method_parameters (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    methodology_id UUID NOT NULL,
    logical_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    value NUMERIC(24,12),
    unit TEXT NOT NULL,
    formula_key TEXT,
    required_inputs JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    applicability JSONB NOT NULL DEFAULT '{}'::jsonb,
    uncertainty JSONB DEFAULT '{}'::jsonb,
    UNIQUE(id,created_at)
);
CREATE INDEX method_parameters_created_idx ON method_parameters(created_at DESC,id DESC);

CREATE TABLE method_quality_policies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    methodology_id UUID NOT NULL,
    version INTEGER NOT NULL,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    artifact_hash TEXT NOT NULL CHECK(artifact_hash ~ '^[0-9a-f]{64}$'),
    UNIQUE(id,created_at)
);
CREATE INDEX method_quality_policies_created_idx ON method_quality_policies(created_at DESC,id DESC);

CREATE TABLE knowledge_documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    logical_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL,
    publisher TEXT NOT NULL,
    source_version TEXT NOT NULL,
    license TEXT NOT NULL,
    source_url TEXT,
    source_page TEXT,
    source_section TEXT,
    source_evidence_id UUID,
    published_date DATE,
    valid_from DATE,
    valid_to DATE,
    geography_codes TEXT[] NOT NULL DEFAULT '{}',
    applicability_notes TEXT NOT NULL,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK(status IN ('DRAFT','APPROVED','ACTIVE','DEACTIVATED','SUPERSEDED')),
    artifact_hash TEXT NOT NULL CHECK(artifact_hash ~ '^[0-9a-f]{64}$'),
    author_id UUID NOT NULL,
    reviewer_id UUID,
    validated_at TIMESTAMPTZ,
    supersedes_id UUID,
    checksum TEXT CHECK(checksum ~ '^[0-9a-f]{64}$'),
    extraction_quality JSONB DEFAULT '{}'::jsonb,
    superseded_by UUID,
    UNIQUE(id,created_at)
);
CREATE INDEX knowledge_documents_created_idx ON knowledge_documents(created_at DESC,id DESC);

CREATE TABLE knowledge_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    document_id UUID NOT NULL,
    document_version INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    page_start INTEGER,
    page_end INTEGER,
    section TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    text_hash TEXT NOT NULL CHECK(text_hash ~ '^[0-9a-f]{64}$'),
    lexical_search_vector TSVECTOR NOT NULL DEFAULT ''::tsvector,
    UNIQUE(id,created_at)
);
CREATE INDEX knowledge_chunks_created_idx ON knowledge_chunks(created_at DESC,id DESC);

CREATE TABLE knowledge_corpus_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
    manifest_hash TEXT NOT NULL CHECK(manifest_hash ~ '^[0-9a-f]{64}$'),
    ingestion_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    retrieval_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    pipeline_version TEXT NOT NULL,
    published_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK(status IN ('DRAFT','APPROVED','ACTIVE','DEACTIVATED','SUPERSEDED')),
    author_id UUID NOT NULL,
    reviewer_id UUID,
    UNIQUE(id,created_at)
);
CREATE INDEX knowledge_corpus_versions_created_idx ON knowledge_corpus_versions(created_at DESC,id DESC);

CREATE TABLE knowledge_embeddings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    chunk_id UUID NOT NULL,
    corpus_version UUID NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_version TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    embedding vector NOT NULL,
    UNIQUE(id,created_at)
);
CREATE INDEX knowledge_embeddings_created_idx ON knowledge_embeddings(created_at DESC,id DESC);

CREATE TABLE institution_deployment_policies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
    data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin IN ('LIVE','SYNTHETIC')),
    logical_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    artifact_hash TEXT NOT NULL CHECK(artifact_hash ~ '^[0-9a-f]{64}$'),
    author_id UUID NOT NULL,
    reviewer_id UUID,
    status TEXT NOT NULL CHECK(status IN ('DRAFT','APPROVED','ACTIVE','DEACTIVATED','SUPERSEDED')),
    approved_at TIMESTAMPTZ,
    UNIQUE(id,created_at)
);
CREATE INDEX institution_deployment_policies_created_idx ON institution_deployment_policies(created_at DESC,id DESC);
