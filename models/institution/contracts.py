"""Compiled v4 wire contracts from the checked-in BSDD field dictionary.

The dictionary is generated offline, reviewed in version control and independent
of the deployment's documentation files. Every named contract is a strict
Pydantic model, so requests and OpenAPI use the same schema.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal, ForwardRef

from pydantic import Field, create_model, model_validator

from .common import StrictModel, UUIDText, DecimalText, HashText, DateText, TimestampText

ENUMS = {
    "RegistrationStatus": "DRAFT SUBMITTED UNDER_REVIEW NEEDS_CORRECTION APPROVED REJECTED",
    "InstitutionStatus": "PENDING APPROVED REJECTED SUSPENDED", "MembershipStatus": "PENDING ACTIVE REVOKED",
    "Permission": "OWNER MANAGER VIEWER", "CampusControl": "PENDING APPROVED REJECTED ARCHIVED",
    "GeometryReview": "PENDING_REVIEW APPROVED NEEDS_CORRECTION", "SurveyStatus": "DRAFT SUBMITTED UNDER_REVIEW ACCEPTED NEEDS_CORRECTION",
    "AuditStatus": "DRAFT SUBMITTED UNDER_REVIEW NEEDS_CORRECTION FINALIZED SUPERSEDED",
    "ProcessingStatus": "IDLE QUEUED RUNNING SUCCEEDED FAILED NEEDS_DATA BLOCKED_ACCESS CANCELLED",
    "TreeReview": "PENDING ACCEPTED NEEDS_CORRECTION", "DeclarationState": "REPORTED ZERO_CONFIRMED NOT_APPLICABLE MISSING",
    "SourceType": "MEASURED REPORTED ESTIMATED", "EvidenceState": "PENDING_UPLOAD UPLOADED CHECKING READY REJECTED EXPIRED",
    "RegistryStatus": "DRAFT APPROVED ACTIVE DEACTIVATED SUPERSEDED", "QueryStatus": "QUEUED RUNNING READY PARTIAL FAILED BLOCKED_ACCESS",
    "PublicationState": "PRIVATE PUBLISHED WITHDRAWN", "ReviewDecision": "START RETURN APPROVE ACCEPT REJECT SUSPEND RESUME",
    "BalanceStatus": "NET_EMITTER BALANCED_ESTIMATE REMOVAL_SURPLUS", "InvitationStatus": "PENDING ACCEPTED REVOKED EXPIRED",
    "ExportFormat": "PDF CSV JSON", "Scope": "SCOPE_1 SCOPE_2 SCOPE_3", "TreeEventType": "PLANTED DIED REMOVED PRUNED RELOCATED TAG_CORRECTED",
}
ENUM_TYPES = {name: Literal[tuple(values.split())] for name, values in ENUMS.items()}
DEFINITIONS = json.loads(Path(__file__).with_name("definitions.json").read_text(encoding="utf-8"))
DEFINITIONS["RegistryResource"]["fields"]["quality_policy_versions"] = {"type":"SourceVersion[]","presence":"optional","rule":"Published immutable method child policy IDs/hashes."}
DEFINITIONS["DeletionAdminResource"]["fields"]["plan"] = {"type":"object","presence":"optional","rule":"Exact inspectable server-derived policy action plan; hash binds decisions."}
MODELS = {}

DETAILS = {
    "ELECTRICITY": {"grid_code": "TEXT", "geography_code": "TEXT", "meter_id": "TEXT", "included_submeter_ids": "TEXT[]", "total_load_kWh": "Decimal or NULL"},
    "TRANSPORT": {"fuel": "TEXT", "vehicle_class": "TEXT", "consumption_basis": "TEXT", "stock_reconciliation": "StockReconciliation or NULL", "standard_conditions": "StandardConditions or NULL", "density_source_id": "UUID or NULL"},
    "STATIONARY_FUEL": {"fuel": "TEXT", "purpose": "TEXT", "consumption_basis": "TEXT", "stock_reconciliation": "StockReconciliation or NULL", "standard_conditions": "StandardConditions or NULL", "density_source_id": "UUID or NULL", "consumed_cylinder_equivalents": "Decimal or NULL", "verified_net_kg_per_cylinder": "Decimal or NULL", "gas_components": "GasComponent[] or NULL"},
    "WASTE": {"material_code": "TEXT", "pathway": "TEXT", "treatment_operator": "TEXT", "treatment_control": "TEXT", "mass_basis": "TEXT", "feedstock_stream_id": "TEXT", "included_energy_input_ids": "UUID[]", "included_transport_input_ids": "UUID[]", "gas_components": "GasComponent[] or NULL"},
    "WATER": {"process": "TEXT", "energy_source": "TEXT", "meter_id": "TEXT or NULL", "supplier_boundary": "TEXT or NULL", "utilized_water_kL": "Decimal or NULL", "energy_intensity_source_id": "UUID or NULL"},
}
ACTION_FIELDS = {
    "SOLAR": {
        "project": {"generated_kWh": "Decimal or NULL", "self_used_kWh": "Decimal or NULL", "exported_kWh": "Decimal or NULL", "losses_kWh": "Decimal or NULL", "meter_refs": "TEXT[]", "project_component_input_ids": "UUID[]"},
        "baseline": {"displaced_self_kWh": "Decimal or NULL", "eligible_export_kWh": "Decimal or NULL", "displaced_grid_factor_id": "UUID or NULL", "export_factor_id": "UUID or NULL"},
        "attributes": {"ownership": "TEXT", "transferred_attributes": "BOOLEAN or NULL", "assessment_boundary": "TEXT"}},
    "RAINWATER": {
        "project": {"utilized_kL": "Decimal or NULL", "pumping_treatment_input_ids": "UUID[]", "other_component_input_ids": "UUID[]"},
        "baseline": {"source": "TEXT", "route": "TEXT", "energy_intensity_source_id": "UUID or NULL", "energy_factor_id": "UUID or NULL", "process_factor_id": "UUID or NULL", "other_components": "Component[]"},
        "attributes": {"assessment_boundary": "TEXT"}},
    "BIOGAS": {
        "project": {"produced_std_m3": "Decimal or NULL", "used_std_m3": "Decimal or NULL", "standard_conditions": "StandardConditions", "methane_fraction": "Decimal or NULL", "LHV_source_id": "UUID or NULL", "route": "TEXT", "efficiency_source_id": "UUID or NULL", "useful_output": "UsefulOutput or NULL", "project_component_input_ids": "UUID[]", "feedstock_input_ids": "UUID[]"},
        "baseline": {"displaced_fuel": "TEXT", "heating_value_source_id": "UUID or NULL", "efficiency_source_id": "UUID or NULL", "energy_factor_id": "UUID or NULL", "treatment_components": "Component[]", "selected_boundary": "TEXT"},
        "attributes": {"assessment_boundary": "TEXT"}},
    "COMPOSTING": {
        "project": {"feedstock_input_ids": "UUID[]", "mass_basis": "TEXT", "actual_treatment_input_ids": "UUID[]", "other_component_input_ids": "UUID[]"},
        "baseline": {"disposal_pathway": "TEXT", "disposal_factor_id": "UUID or NULL", "other_components": "Component[]", "evidence_ids": "UUID[]"},
        "attributes": {"assessment_boundary": "TEXT"}},
}

for name, fields in DETAILS.items():
    DEFINITIONS[name + "Details"] = {"fields": {k: {"type": v, "presence": "required", "rule": "BSDD 18.11"} for k, v in fields.items()}}
for action, groups in ACTION_FIELDS.items():
    for group, fields in groups.items():
        DEFINITIONS[action + group.title()] = {"fields": {k: {"type": v, "presence": "required", "rule": "BSDD 18.11"} for k, v in fields.items()}}
DEFINITIONS["UsefulOutput"] = {"fields": {k: {"type": v, "presence": "required", "rule": ""} for k, v in {"value": "Decimal", "unit": "TEXT", "source": "TEXT", "evidence_ids": "UUID[]"}.items()}}
DEFINITIONS["CampusStratum"] = {"fields": {k: {"type": v, "presence": "required", "rule": "Site-specific sampling stratum; evidence optional for administrator configuration"} for k, v in {"name": "TEXT", "geometry": "GeoJSON MultiPolygon", "evidence_id": "UUID or NULL", "inclusion_probability": "Decimal", "design_weight": "Decimal"}.items()}}
DEFINITIONS["CampusProcessingConfig"] = {"fields": {k: {"type": v, "presence": "required", "rule": "Actual immutable raster/stratum input; no guessed geometry or source"} for k, v in {"mask_engine": "TEXT", "mask_asset_id": "TEXT", "mask_asset_update_time": "TEXT", "mask_tree_value": "INTEGER", "mask_source_date": "DATE", "mask_native_resolution_m": "Decimal", "mask_validation_evidence_id": "UUID or NULL", "strata": "CampusStratum[]"}.items()}}
DEFINITIONS["MethodConfig"]["fields"]["campus_processing"] = {"type": "CampusProcessingConfig or NULL", "presence": "optional", "rule": "Required for campus mask/plot jobs; optional for operational accounting"}
for name, fields in {
    "ImportPreviewRequest": {"evidence_id": "UUID", "category": "TEXT", "column_mapping": "map<TEXT,TEXT>", "expected_input_revision": "INTEGER", "operation": "TEXT"},
    "ImportCommitRequest": {"preview_id": "UUID", "normalized_hash": "SHA-256", "expected_input_revision": "INTEGER"},
    "ReviewClaimRequest": {"current_revision": "INTEGER", "action": "TEXT"},
    "RegistrationAuthorizationRequest": {"target_user_id": "UUID", "reason": "TEXT"},
}.items():
    DEFINITIONS[name] = {"fields": {key: {"type": kind, "presence": "optional" if name == "ReviewClaimRequest" and key == "action" else "required", "rule": ">0" if key.endswith("revision") else "1..2000 characters" if key == "reason" else "Exact documented administrative/import request"} for key, kind in fields.items()}}


def wire_type(description):
    description = description.strip()
    if description.startswith("GeoJSON") and description.endswith(" or NULL"):
        return dict[str,Any] | None
    if description.startswith("map<"):
        inner = description[4:-1].split(",", 1)[1]
        return dict[str, wire_type(inner)]
    if " or " in description and not description.startswith("GeoJSON"):
        types = description.split(" or ")
        result = wire_type(types[0])
        for item in types[1:]:
            result = result | (type(None) if item == "NULL" else wire_type(item))
        return result
    if description.endswith("[]"):
        return list[wire_type(description[:-2])]
    if description.startswith("Partial<"):
        return ForwardRef("Partial" + description[8:-1])
    if description == "Page<ImportRow>":
        return ForwardRef("ImportRowPage")
    if description.startswith("GeoJSON") or description in {"JSON Schema", "object", "category-specific object", "action-specific object", "DraftRegistryConfig", "RegistryConfig"}:
        return dict[str, Any]
    base = {"TEXT": str, "UUID": UUIDText, "SHA-256": HashText, "Decimal": DecimalText,
            "DATE": DateText, "timestamp": TimestampText, "BOOLEAN": bool, "INTEGER": int,
            "HTTPS URL": str, "EmissionLine.unit": Literal["kWh", "L", "kg", "std_m3", "m3", "kL", "t", "MJ"], "T": Any}
    if description in base:
        return base[description]
    if description in ENUM_TYPES:
        return ENUM_TYPES[description]
    if description in DEFINITIONS:
        return ForwardRef("InstitutionWarning" if description == "Warning" else description)
    raise RuntimeError(f"Unresolved v4 contract type: {description}")


def inherited_fields(name):
    definition = DEFINITIONS[name]
    parent = definition.get("extends")
    return {**(inherited_fields(parent) if parent else {}), **definition["fields"]}


def semantic_validation(self):
    name = type(self).__name__
    from decimal import Decimal
    values = self.model_dump(exclude_unset=True)
    fields = inherited_fields(name) if name in DEFINITIONS else {}
    for key, value in values.items():
        rule = fields.get(key, {}).get("rule", "")
        if value is None:
            continue
        if key == "schema_version" and value != 1:
            raise ValueError("Only schema_version=1 is supported.")
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            if rule.startswith((">=0", ">0", "[0,1]", "(0,1]", "(0,1)")):
                number = Decimal(str(value))
                if rule.startswith(">=0") and number < 0 or rule.startswith(">0") and number <= 0 or rule.startswith("[0,1]") and not 0 <= number <= 1 or rule.startswith("(0,1]") and not 0 < number <= 1 or rule.startswith("(0,1)") and not 0 < number < 1:
                    raise ValueError(f"{key} violates its approved range.")
        match = re.match(r"1(?:–|\.\.|-)(\d+) characters", rule)
        if match and isinstance(value, str) and not 1 <= len(value.strip()) <= int(match[1]):
            raise ValueError(f"{key} violates its length constraint.")
    choices = {
        "ProfileData": {"type":"UNIVERSITY COLLEGE SCHOOL HOSPITAL OFFICE OTHER", "convention":"CALENDAR FINANCIAL ACADEMIC CUSTOM"},
        "TreeCreate": {"condition":"LIVE DEAD REMOVED UNKNOWN"}, "TreePatch": {"condition":"LIVE DEAD REMOVED UNKNOWN"},
        "MeasurementItem": {"height_source":"AR FIELD_MEASURED APPROVED_MODEL UNKNOWN", "species_selection":"CLASSIFIER MANUAL EXPERT UNKNOWN", "measurement_form":"DIAMETER CIRCUMFERENCE MULTISTEM"},
        "FactorConfig": {"purpose":"INVENTORY AVOIDED", "gas_basis":"COMBINED_CO2E GAS_SPECIFIC"},
        "ModelConfig": {"engine":"GEE_SMILE_GBT XGBOOST", "mode":"PRETRAINED_DOMAIN APPROVED_CAMPUS_CALIBRATION", "target_unit":"tAGB/ha", "missing_sensor_strategy":"NEEDS_DATA REDUCED_MODEL"},
        "BoundaryCreate": {"source":"UPLOADED OPERATOR_DIGITIZED"},
        "UncertaintyPolicy": {"method_key":"UNKNOWN PROPAGATED_BOUNDS VALIDATED_JOINT", "unknown_handling":"REPORT_UNKNOWN"},
        "QualityUncertainty": {"procedure":"UNKNOWN PROPAGATED_BOUNDS VALIDATED_JOINT"},
        "RankingConfig": {"mode":"RULES_V1", "within_class":"CATEGORY_SHARE_DESC_THEN_ACTION_ID_ASC", "unknown_share":"LAST"},
        "KnowledgeConfig": {"source_type":"URL UPLOAD", "visibility":"PUBLIC_APPROVED"},
        "ImportPreviewRequest": {"operation":"APPEND_NEW", "category":"ELECTRICITY TRANSPORT STATIONARY_FUEL WASTE WATER"},
        "ReviewClaimRequest": {"action":"CLAIM RENEW RELEASE"},
    }
    for key, options in choices.get(name, {}).items():
        if key in values and values[key] not in options.split():
            raise ValueError(f"Unsupported {key}.")
    if name == "EmissionLine":
        model = MODELS.get(self.category + "Details")
        if model is None:
            raise ValueError("Unsupported activity category.")
        model.model_validate(self.details)
    if name == "MitigationLine":
        if self.action not in ACTION_FIELDS:
            raise ValueError("Unsupported mitigation action.")
        for group in ACTION_FIELDS[self.action]:
            MODELS[self.action + group.title()].model_validate(getattr(self, group))
    if name == "MeasurementItem":
        from .common import measurement_hash
        raw = self.model_dump(exclude_unset=True)
        if measurement_hash(raw) != self.client_content_hash:
            raise ValueError("MCJ1 content hash differs from raw capture.")
        if self.ar_tier not in (1, 2, 3) or self.capture_quality.ar_tier != self.ar_tier:
            raise ValueError("AR tier must agree with capture quality.")
        if self.location is None and (self.gps_accuracy_m is not None or self.capture_quality.gps_recorded_at is not None or "GPS_NOT_CAPTURED" not in self.capture_quality.capture_flags):
            raise ValueError("Missing GPS requires null accuracy/time and GPS_NOT_CAPTURED.")
        if self.location is not None:
            from services.shared.sampling_service import point
            point(self.location)
        if self.measurement_form == "CIRCUMFERENCE" and self.dbh_cm is not None:
            raise ValueError("Circumference capture stores raw circumference; derived DBH is a separate server field.")
        if self.measurement_form != "MULTISTEM" and self.stems:
            raise ValueError("Stem measurements require the declared multistem form.")
    for field in ("replace", "read", "acknowledge_retention"):
        if hasattr(self, field) and getattr(self, field) is not True:
            raise ValueError(f"{field} must be true.")
    if name in {"MeasurementBatch", "EmissionReplace", "MitigationReplace"} and len(self.items) > 200:
        raise ValueError("Batch limit is 200.")
    if name.endswith("Patch") and not self.model_fields_set:
        raise ValueError("At least one changed field is required.")
    return self


for name in DEFINITIONS:
    if "<" in name:
        continue
    fields = {}
    for key, spec in inherited_fields(name).items():
        optional = spec["presence"] in {"optional", "conditional"}
        default = None if optional else ...
        fields[key] = (wire_type(spec["type"]), Field(default=default, description=spec["rule"]))
    MODELS[name] = create_model(name, __base__=StrictModel, __module__=__name__,
                              __validators__={"semantic_validation": model_validator(mode="after")(semantic_validation)}, **fields)

for name in ("ProfileData", "AddressData", "ContactData"):
    fields = {}
    for key, spec in inherited_fields(name).items():
        kind = spec["type"]
        if kind in {"AddressData", "ContactData"}:
            kind = "Partial<" + kind + ">"
        fields[key] = (wire_type(kind), None)
    MODELS["Partial" + name] = create_model("Partial" + name, __base__=StrictModel, __module__=__name__, **fields)
MODELS["ImportRowPage"] = create_model("ImportRowPage", __base__=StrictModel, items=(list[ForwardRef("ImportRow")], ...), next_cursor=(str | None, ...), total=(int | None, ...))
MODELS["InstitutionWarning"] = MODELS["Warning"]
globals().update(MODELS)
for model in MODELS.values():
    model.model_rebuild(_types_namespace=MODELS)
globals().update(MODELS)


def parse(name: str, body: dict, *, partial=False):
    """Preserve omission and raw decimal spelling after strict validation."""
    model = MODELS[name]
    if partial:
        fields = {k: (f.annotation, None) for k, f in model.model_fields.items()}
        model = create_model(name + "Partial", __base__=StrictModel, **fields)
    return model.model_validate(body).model_dump(exclude_unset=True)
