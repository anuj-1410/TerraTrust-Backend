"""Strict checkpoint manifests and a fixed target-owned table allowlist."""

from typing import Literal
from pydantic import model_validator

from models.institution.common import (
    StrictModel,
    UUIDText,
    HashText,
    TimestampText,
    digest,
    measurement_hash,
)
from repositories.institution.base import TABLES, columns
from demo.institution.blueprints import KEYS

EXCLUDED = set(
    "users emission_factors carbon_methodologies model_registry method_parameters method_quality_policies knowledge_documents knowledge_chunks knowledge_embeddings knowledge_corpus_versions institution_deployment_policies operator_grants idempotency_records institution_deletion_requests institution_registration_authorizations job_outbox".split()
)
EXCLUDED.update(
    {
        "institution_calculation_settings",
        "institution_formula_values",
        "institution_knowledge_sources",
    }
)
ALLOWED = set(TABLES) - EXCLUDED


class CheckpointTable(StrictModel):
    table: str
    rows: list[dict]
    row_hashes: list[HashText]


class CheckpointObject(StrictModel):
    bucket: str
    object_key: str
    mime_type: str
    size_bytes: int
    sha256: HashText
    source_object_key: str
    parent_type: str
    parent_id: UUIDText


class Prerequisites(StrictModel):
    method_ids: list[UUIDText]
    factor_ids: list[UUIDText]
    model_ids: list[UUIDText]
    quality_policy_ids: list[UUIDText]
    corpus_ids: list[UUIDText]
    config_hash: HashText


class UserCheckpoint(StrictModel):
    onboarding_status: Literal["REQUIRED", "PENDING_REVIEW", "REJECTED", "COMPLETE"]
    onboarding_completed_at: TimestampText | None
    registration_id: UUIDText | None
    registration_institution_id: UUIDText | None


class Expected(StrictModel):
    landing_screen: str
    permission: Literal["OWNER"] | None
    tenant_count: int
    campus_count: int
    survey_count: int
    tree_audit_count: int
    audit_count: int
    current_final_audit_id: UUIDText | None
    result_hash: HashText | None
    export_ids: list[UUIDText]
    published_query_ids: list[UUIDText]


class Provenance(StrictModel):
    prepared_via_normal_services: Literal[True]
    review_case_ids: list[UUIDText]
    source_record_ids: list[UUIDText]
    validation_record_id: UUIDText


class CheckpointManifest(StrictModel):
    schema_version: Literal[1]
    manifest_id: UUIDText
    manifest_version: int
    checkpoint_key: str
    account_number: Literal[5, 6, 7, 8, 9]
    target_firebase_uid: str
    target_user_id: UUIDText
    deployment_id: str
    deployment_env: Literal["development", "test"]
    owned_tenant_ids: list[UUIDText]
    created_at: TimestampText
    created_by: UUIDText
    prerequisites: Prerequisites
    user_checkpoint: UserCheckpoint
    tables: list[CheckpointTable]
    objects: list[CheckpointObject]
    expected: Expected
    provenance: Provenance
    manifest_sha256: HashText

    @model_validator(mode="after")
    def verify_bundle(self):
        data = self.model_dump()
        if (
            digest({k: v for k, v in data.items() if k != "manifest_sha256"})
            != self.manifest_sha256
        ):
            raise ValueError("Manifest checksum differs from the immutable source.")
        if (
            self.checkpoint_key != KEYS[self.account_number]
            or self.manifest_version < 1
        ):
            raise ValueError("Invalid account/checkpoint/version mapping.")
        seen_tables, seen_ids, counts = set(), set(), {}
        owned = set(self.owned_tenant_ids)
        for bundle in self.tables:
            if bundle.table not in ALLOWED or bundle.table in seen_tables:
                raise ValueError("Unknown, excluded or duplicate checkpoint table.")
            seen_tables.add(bundle.table)
            if len(bundle.rows) != len(bundle.row_hashes):
                raise ValueError("Row checksums do not match bundle rows.")
            counts[bundle.table] = len(bundle.rows)
            for row, expected_hash in zip(bundle.rows, bundle.row_hashes):
                if (
                    "period_range" in row
                    or set(row) - columns(bundle.table)
                    or digest(row) != expected_hash
                    or (bundle.table, row.get("id")) in seen_ids
                ):
                    raise ValueError("Invalid checkpoint row/column/checksum/identity.")
                seen_ids.add((bundle.table, row.get("id")))
                own_pretenant = (
                    bundle.table == "institution_audit_evidence"
                    and row.get("institution_id") is None
                    and row.get("parent_type") == "REGISTRATION_DRAFT"
                    and row.get("parent_id") == self.target_user_id
                    and row.get("owner_user_id") == self.target_user_id
                )
                if (
                    row["id"]
                    if bundle.table == "institutions"
                    else row.get("institution_id")
                ) not in owned and not own_pretenant:
                    raise ValueError(
                        "Checkpoint contains a row outside its dedicated target scope."
                    )
                if bundle.table == "processing_jobs" and row["status"] != "SUCCEEDED":
                    raise ValueError(
                        "Only actual successful historical jobs may be captured."
                    )
                if bundle.table == "review_cases" and (
                    row["status"] != "CLOSED"
                    or row["decision"] not in {"APPROVE", "ACCEPT"}
                ):
                    raise ValueError(
                        "Only actual independently completed reviews may be captured."
                    )
                if (
                    bundle.table == "campus_tree_measurements"
                    and measurement_hash(row["raw_input"]) != row["content_hash"]
                ):
                    raise ValueError("Raw measurement checksum differs.")
        for obj in self.objects:
            if (
                not obj.source_object_key.startswith(
                    "institution-checkpoints/" + self.deployment_id + "/objects/"
                )
                or obj.size_bytes <= 0
                or obj.size_bytes > 20971520
                or "?" in obj.object_key
            ):
                raise ValueError(
                    "Private source object is outside the immutable bundle or size contract."
                )
        for table, field in {
            "institutions": "tenant_count",
            "campuses": "campus_count",
            "campus_tree_surveys": "survey_count",
            "campus_tree_audits": "tree_audit_count",
            "institution_audits": "audit_count",
        }.items():
            if counts.get(table, 0) != getattr(self.expected, field):
                raise ValueError(
                    "Checkpoint expected counts differ from captured content."
                )
        if self.account_number == 5 and (
            owned
            or any(counts.values())
            or self.objects
            or self.user_checkpoint.onboarding_status != "REQUIRED"
        ):
            raise ValueError("Account five must start without tenant content.")
        if self.account_number >= 6 and (
            self.expected.permission != "OWNER" or self.expected.tenant_count != 1
        ):
            raise ValueError("Prepared checkpoints require dedicated owner scope.")
        if self.account_number == 6 and any(
            counts.get(t, 0)
            for t in ("campuses", "institution_audits", "campus_tree_surveys")
        ):
            raise ValueError(
                "Approved-registration checkpoint cannot contain campus/audit content."
            )
        if self.account_number == 7 and (
            not counts.get("campuses")
            or any(
                counts.get(t, 0)
                for t in (
                    "campus_tree_measurements",
                    "campus_tree_surveys",
                    "campus_tree_audits",
                    "institution_audits",
                    "advisory_queries",
                )
            )
        ):
            raise ValueError("Campus-ready checkpoint has incorrect captured stage.")
        if self.account_number >= 7 and (
            self.expected.campus_count != 1
            or not counts.get("campus_trees")
            or not counts.get("campus_sampling_plots")
        ):
            raise ValueError(
                "Campus checkpoints require one real reviewed campus, plots and stable tree tags."
            )
        if self.account_number == 8 and (
            self.expected.survey_count != 1
            or self.expected.tree_audit_count != 1
            or any(
                counts.get(t, 0)
                for t in (
                    "institution_audits",
                    "institution_emission_inputs",
                    "institution_mitigation_inputs",
                    "institution_exports",
                    "advisory_queries",
                )
            )
        ):
            raise ValueError(
                "Biomass checkpoint requires one accepted baseline with operational forms and annual audits pending."
            )
        if self.account_number == 9 and (
            self.expected.audit_count != 2
            or not self.expected.current_final_audit_id
            or not self.expected.result_hash
            or self.expected.tree_audit_count != 2
        ):
            raise ValueError(
                "Persistent checkpoint requires both completed years and two actual stock assessments."
            )
        return self
