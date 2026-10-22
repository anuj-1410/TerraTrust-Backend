"""Repeatable sample checkpoints in normal tables; never simulated live services.

Historical stock/review states are illustrative SYNTHETIC fixtures, not field or
satellite observations. Annual arithmetic and export bytes use the real services.
"""

import hashlib
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from random import Random
from uuid import NAMESPACE_URL, uuid5

from app.config import settings
from app.institution_errors import fail
from demo.institution.blueprints import KEYS, phone_for
from demo.institution.manifest import CheckpointTable, UserCheckpoint
from models.institution.common import digest, measurement_hash, normalize, numeric
from repositories.institution.base import TABLES

STAMP = "2026-01-01T00:00:00.000000Z"
NOTICE = "SYNTHETIC DEMO SAMPLE: illustrative inputs and historical states; not field/satellite verification or independent certification."


def identifier(key):
    return str(uuid5(NAMESPACE_URL, "terratrust:institution:sample:v1:" + key))


def row(table, key, **values):
    nullable = {name: None for name, spec in TABLES[table].items() if spec["nullable"]}
    result = {
        **nullable,
        "id": identifier(key),
        "created_at": STAMP,
        "updated_at": STAMP,
        "input_revision": 1,
        "data_origin": "SYNTHETIC",
        **values,
    }
    for name, value in result.items():
        kind = TABLES[table].get(name, {}).get("type", "")
        if value is not None and kind.startswith("NUMERIC"):
            result[name] = numeric(Decimal(str(value)))
        elif value is not None and kind == "TIMESTAMPTZ":
            result[name] = normalize(datetime.fromisoformat(value))
    return result


@dataclass
class SampleObject:
    bucket: str
    object_key: str
    data: bytes
    mime_type: str

    @property
    def sha256(self):
        return hashlib.sha256(self.data).hexdigest()

    @property
    def size_bytes(self):
        return len(self.data)


@dataclass
class SampleCheckpoint:
    account_number: int
    target_firebase_uid: str
    target_user_id: str | None = None
    owned_tenant_ids: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    objects: list = field(default_factory=list)
    globals: list = field(default_factory=list)
    user_checkpoint: UserCheckpoint | None = None

    @property
    def checkpoint_key(self):
        return KEYS[self.account_number]


def build_sample(account, firebase_uid, user_id):
    """Fixed random seed makes each reset restore the same sample inputs."""
    from services.institution.accounting_service import CATEGORIES, calculate_inventory
    from services.institution.calculation_defaults import normal_settings
    from services.institution.report_service import render
    from services.institution.runtime_configuration import configuration

    if account not in {6, 7, 8, 9}:
        raise ValueError("Sample stages are defined only for institution accounts 6-9.")
    sample = SampleCheckpoint(account, firebase_uid, user_id)
    prefix = f"account-{account}:{firebase_uid}:"
    uid = lambda name: identifier(prefix + name)
    iid, cid = uid("institution"), uid("campus")
    sample.owned_tenant_ids = [iid]
    sample.user_checkpoint = UserCheckpoint(
        onboarding_status="COMPLETE",
        onboarding_completed_at=STAMP,
        registration_id=uid("registration"),
        registration_institution_id=iid,
    )
    data = {}

    def add(table, row_key, **values):
        value = row(
            table,
            prefix + row_key,
            **({"institution_id": iid} if table not in {"institutions"} else {}),
            **values,
        )
        data.setdefault(table, []).append(value)
        return value

    factor_configs = [
        ("grid", "ELECTRICITY", "GRID", "GRID", None, None, "kWh", "0.71", "SCOPE_2"),
        (
            "bus",
            "TRANSPORT",
            "OWNED_FLEET",
            "BUS",
            "DIESEL",
            None,
            "L",
            "2.68",
            "SCOPE_1",
        ),
        (
            "generator",
            "STATIONARY_FUEL",
            "DIESEL",
            "GENERATOR",
            "DIESEL",
            None,
            "L",
            "2.68",
            "SCOPE_1",
        ),
        ("waste", "WASTE", "FOOD", "LANDFILL", None, "FOOD", "t", "100", "SCOPE_3"),
        ("water", "WATER", "SUPPLY", "SUPPLY", None, None, "kL", "0.5", "SCOPE_3"),
    ]
    factor_inputs = []
    for (
        key,
        category,
        subcategory,
        process,
        fuel,
        material,
        unit,
        value,
        scope,
    ) in factor_configs:
        config = {
            "category": category,
            "subcategory": subcategory,
            "process": process,
            "fuel": fuel,
            "material": material,
            "numerator_unit": "kgCO2e",
            "denominator_unit": unit,
            "gas_basis": "COMBINED_CO2E",
            "scope": scope,
            "purpose": "INVENTORY",
            "boundary": NOTICE,
            "exclusions": [],
            "mass_basis": "WET" if key == "waste" else None,
            "standard_conditions": None,
            "gwp": None,
            "uncertainty": None,
        }
        factor_inputs.append(
            {
                "logical_key": "SAMPLE_" + key.upper(),
                "value": value,
                "config": config,
                "source_label": NOTICE,
                "geography_codes": ["IN"],
                "valid_from": None,
                "valid_to": None,
                "enabled": True,
            }
        )
    solar = deepcopy(factor_inputs[0])
    solar.update(logical_key="SAMPLE_AVOIDED_GRID")
    solar["config"]["purpose"] = "AVOIDED"
    factor_inputs.append(solar)
    _, method_config, resolved = configuration(
        {"config": normal_settings(), "updated_at": STAMP}, factor_inputs
    )
    mid, qid, model_id = [
        identifier("global:" + name) for name in ("method", "quality", "sample-model")
    ]

    def registry(table, key, config):
        return row(
            table,
            "global:" + key,
            logical_key="SAMPLE_DEMO_" + key.upper(),
            version=1,
            schema_version=1,
            title="Demonstration sample " + key,
            publisher="TerraTrust sample generator",
            source_version="SAMPLE_V1",
            license="PROJECT_SAMPLE",
            applicability_notes=NOTICE,
            config=config,
            status="APPROVED",
            artifact_hash=digest(config),
            author_id=user_id,
            geography_codes=["IN"],
            validated_at=STAMP,
        )

    sample.globals.append(
        (
            "carbon_methodologies",
            registry("carbon_methodologies", "method", method_config),
        )
    )
    for parameter in method_config["parameters"]:
        sample.globals.append(
            (
                "method_parameters",
                row(
                    "method_parameters",
                    "parameter:" + parameter["id"],
                    methodology_id=mid,
                    **parameter,
                ),
            )
        )
    sample.globals.append(
        (
            "method_quality_policies",
            row(
                "method_quality_policies",
                "global:quality",
                methodology_id=mid,
                version=1,
                config=method_config["quality_policy"],
                artifact_hash=digest(method_config["quality_policy"]),
            ),
        )
    )
    sample.globals.append(
        (
            "model_registry",
            registry(
                "model_registry",
                "sample-model",
                {"engine": "ILLUSTRATIVE_SAMPLE_STOCK", "notice": NOTICE},
            ),
        )
    )
    factors = {}
    factor_ids = {}
    for fid, source, config in resolved:
        factor = registry("emission_factors", "factor:" + source["logical_key"], config)
        factor.update(id=fid, logical_key="SAMPLE_DEMO_" + source["logical_key"])
        sample.globals.append(("emission_factors", factor))
        # Snapshot-only source checksum of the sample configuration bytes;
        # source_byte_hash is not a physical emission_factors column.
        factors[fid] = {**factor, "source_byte_hash": digest(config)}
        factor_ids[source["logical_key"]] = fid

    address = {
        "line1": "Sample campus address",
        "line2": None,
        "city": "Nagpur",
        "state": "Maharashtra",
        "postal_code": "440013",
        "country": "IN",
    }
    profile = {
        "name": f"Demo Institution {account} - Sample Data",
        "type": "COLLEGE",
        "address": address,
        "contact": {
            "name": "Demo Administrator",
            "phone_e164": phone_for(account),
            "email": f"demo{account}@example.invalid",
        },
        "reporting_timezone": "Asia/Kolkata",
        "convention": "CALENDAR",
        "student_count": 600,
        "staff_count": 75,
        "floor_area_m2": "12000",
    }
    add(
        "institutions",
        "institution",
        created_by=user_id,
        profile=profile,
        consolidation_approach="OPERATIONAL_CONTROL",
        verification_status="APPROVED",
        current_registration_id=uid("registration"),
        review_reason=NOTICE,
    )
    add(
        "institution_users",
        "owner",
        user_id=user_id,
        permission="OWNER",
        status="ACTIVE",
        added_by=user_id,
        joined_at=STAMP,
    )
    add(
        "institution_registrations",
        "registration",
        logical_registration_id=uid("registration-logical"),
        revision=1,
        created_by=user_id,
        status="APPROVED",
        profile_snapshot=profile,
        input_hash=digest(profile),
        frozen_input_hash=digest(profile),
        evidence_ids=[],
        review_reason=NOTICE,
    )
    if account >= 7:
        # Sample geometry is deliberately small and clearly labelled, not a claim
        # about the ownership or vegetation of an actual campus.
        x, y = 79.06 + (account - 7) * 0.01, 21.17

        def square(px, py, width):
            px, py = round(px, 9), round(py, 9)
            right, top = round(px + width, 9), round(py + width, 9)
            return {
                "type": "MultiPolygon",
                "coordinates": [
                    [[[px, py], [right, py], [right, top], [px, top], [px, py]]]
                ],
            }

        geometry = square(x, y, 0.004)
        area = "18.4"
        add(
            "campuses",
            "campus",
            name=f"Sample Campus {account}",
            address=address,
            created_by=user_id,
            control_status="APPROVED",
            control_evidence_ids=[],
            control_input_hash=digest(geometry),
            control_approved_hash=digest(geometry),
            current_boundary_id=uid("boundary"),
            current_mask_id=uid("mask"),
            current_plot_set_id=uid("plotset"),
        )
        add(
            "campus_boundaries",
            "boundary",
            campus_id=cid,
            revision=1,
            geometry=geometry,
            geometry_hash=digest(geometry),
            area_ha=area,
            effective_start="2024-01-01",
            source="SYNTHETIC_SAMPLE",
            evidence_ids=[],
            created_by=user_id,
            review_status="APPROVED",
            input_hash=digest(geometry),
            review_reason=NOTICE,
        )
        add(
            "campus_vegetation_masks",
            "mask",
            campus_id=cid,
            boundary_id=uid("boundary"),
            method_version=mid,
            revision=1,
            created_by=user_id,
            observation_start="2024-01-01",
            observation_end="2025-12-31",
            eligible_geometry=geometry,
            eligible_area_ha=area,
            parameters={"sample": True, "notice": NOTICE},
            coverage={"sample": True},
            evidence_ids=[],
            input_hash=digest(geometry),
            processing_status="SUCCEEDED",
            review_status="APPROVED",
            review_reason=NOTICE,
        )

        def job(name, kind, resource):
            return add(
                "processing_jobs",
                name,
                domain="INSTITUTION",
                asset_type="CAMPUS",
                resource_id=resource,
                requested_by=user_id,
                job_type=kind,
                source_input_revision=1,
                input_hash=digest(name),
                status="SUCCEEDED",
                stage="SAMPLE_PRELOADED",
                progress=100,
                max_attempts=1,
                attempt=1,
                frozen_input={"sample": True, "notice": NOTICE},
                stage_outputs={"sample": True},
                provider_task_ids={},
                retryable=False,
                started_at=STAMP,
                finished_at=STAMP,
                deadline_at="2099-01-01T00:00:00Z",
                output_ref=resource,
            )

        job("plot-job", "campus_plot_generation", uid("plotset"))
        add(
            "campus_plot_sets",
            "plotset",
            campus_id=cid,
            boundary_id=uid("boundary"),
            mask_id=uid("mask"),
            quality_policy_version=qid,
            revision=1,
            created_by=user_id,
            design={"sample": True, "strata": ["TREE_AREA"]},
            review_status="APPROVED",
            input_hash=digest(geometry),
            generation_job_id=uid("plot-job"),
            review_reason=NOTICE,
        )
        for plot in range(3):
            add(
                "campus_sampling_plots",
                f"plot-{plot}",
                campus_id=cid,
                plot_set_id=uid("plotset"),
                boundary_id=uid("boundary"),
                mask_id=uid("mask"),
                quality_policy_version=qid,
                geometry=square(x + 0.0005 + plot * 0.001, y + 0.001, 0.000095),
                measured_area_ha="0.01",
                stratum="TREE_AREA",
                inclusion_rule={
                    "inclusion_probability": "1",
                    "design_weight": "1",
                    "sample": True,
                },
                active_start="2024-01-01",
                review_status="APPROVED",
            )
        random = Random(4100 + account)
        trees = []
        for index in range(12):
            location = {
                "type": "Point",
                "coordinates": [
                    round(
                        x + 0.000515 + (index // 4) * 0.001 + (index % 4) * 0.000015, 9
                    ),
                    round(y + 0.00103, 9),
                ],
            }
            tree = add(
                "campus_trees",
                f"tree-{index}",
                campus_id=cid,
                stable_tag=f"DEMO-{account}-{index + 1:03}",
                location=location,
                species_code="Neem",
                condition="LIVE",
                created_by=user_id,
                metadata={"sample": True},
            )
            trees.append(
                (tree, str(random.randint(15, 35)), str(random.randint(5, 12)))
            )
        stocks = []
        for year in () if account == 7 else (2025,) if account == 8 else (2024, 2025):
            survey_id, stock_id = uid(f"survey-{year}"), uid(f"stock-{year}")
            add(
                "campus_tree_surveys",
                f"survey-{year}",
                campus_id=cid,
                logical_survey_id=survey_id,
                boundary_id=uid("boundary"),
                mask_id=uid("mask"),
                plot_set_id=uid("plotset"),
                quality_policy_version=qid,
                revision=1,
                survey_start=f"{year}-01-01",
                survey_end=f"{year}-12-31",
                created_by=user_id,
                input_hash=digest(survey_id),
                status="ACCEPTED",
                review_reason=NOTICE,
            )
            for index, (tree, dbh, height) in enumerate(trees):
                raw = {
                    "local_uuid": uid(f"measure-{year}-{index}"),
                    "tree_id": tree["id"],
                    "plot_id": uid(f"plot-{index // 4}"),
                    "measured_at": f"{year}-06-01T10:00:00Z",
                    "location": tree["location"],
                    "gps_accuracy_m": "5",
                    "dbh_cm": dbh,
                    "height_m": height,
                    "height_source": "FIELD_MEASURED",
                    "species_code": "Neem",
                    "species_selection": "MANUAL",
                    "species_confidence": None,
                    "ar_tier": 3,
                    "allometry_id": method_config["quality_policy"]["eligibility"][
                        "allometry_ids"
                    ][0],
                    "wood_density_source_id": None,
                    "measurement_height_m": "1.3",
                    "protocol_version": method_config["quality_policy"][
                        "measurement_protocol_version"
                    ],
                    "measurement_form": "DIAMETER",
                    "circumference_cm": None,
                    "stems": [],
                    "evidence_ids": [],
                    "capture_quality": {
                        "camera_native_bridge_version": None,
                        "ar_tier": 3,
                        "classifier_version": None,
                        "height_provider_version": None,
                        "capture_flags": ["SYNTHETIC_SAMPLE"],
                        "measurement_uncertainty": None,
                        "gps_recorded_at": None,
                        "consent_context_id": None,
                    },
                }
                raw["client_content_hash"] = measurement_hash(raw)
                add(
                    "campus_tree_measurements",
                    f"measure-{year}-{index}",
                    campus_id=cid,
                    survey_id=survey_id,
                    created_by=user_id,
                    raw_input=raw,
                    content_hash=measurement_hash(raw),
                    derived_dbh_cm=dbh,
                    validation_status="VALID",
                    validation_issues=[],
                    **{
                        key: value
                        for key, value in raw.items()
                        if key != "client_content_hash"
                    },
                )
            job(f"stock-job-{year}", "campus_fusion", stock_id)
            agb = Decimal(30) + (
                Decimal(3) if year == 2025 and account == 9 else Decimal(0)
            )
            carbon = agb * Decimal("0.47")
            co2 = carbon * Decimal("3.667")
            change = (co2 - Decimal(stocks[0]["stock_co2_t"])) if stocks else None
            stock = add(
                "campus_tree_audits",
                f"stock-{year}",
                campus_id=cid,
                survey_id=survey_id,
                boundary_id=uid("boundary"),
                mask_id=uid("mask"),
                model_version=model_id,
                method_version=mid,
                feature_schema_version=identifier("sample-feature"),
                revision=1,
                assessment_date=f"{year}-12-31",
                observation_start=f"{year}-01-01",
                observation_end=f"{year}-12-31",
                created_by=user_id,
                input_hash=digest(stock_id),
                result_hash=digest({"sample_stock": str(agb)}),
                processing_status="SUCCEEDED",
                review_status="ACCEPTED",
                job_id=uid(f"stock-job-{year}"),
                agb_t=str(agb),
                stock_tC=str(carbon),
                stock_co2_t=str(co2),
                baseline_audit_id=stocks[0]["id"] if stocks else None,
                comparability_status="COMPARABLE" if stocks else "BASELINE_ONLY",
                signed_change_tCO2=str(change) if change is not None else None,
                removal_tCO2=str(max(change, Decimal(0)))
                if change is not None
                else None,
                release_tCO2="0" if change is not None else None,
                quality_flags=["SYNTHETIC_SAMPLE", "NO_SATELLITE_VERIFICATION"],
                provenance={
                    "sample": True,
                    "notice": NOTICE,
                    "quality_policy_version": qid,
                },
                coverage={"sample": True},
                uncertainty={
                    "type": "UNKNOWN",
                    "lower": None,
                    "upper": None,
                    "confidence_level": None,
                    "method_version": None,
                    "dependence_assumptions": None,
                    "sources": [],
                },
                review_reason=NOTICE,
            )
            stocks.append(stock)
            if account != 9:
                continue
            audit_id, snapshot_id = uid(f"audit-{year}"), uid(f"snapshot-{year}")
            coverage = {
                "consolidation_approach": "OPERATIONAL_CONTROL",
                "included_scopes": ["SCOPE_1", "SCOPE_2", "SCOPE_3"],
                "categories": [
                    {
                        "category": c,
                        "state": "REPORTED",
                        "reason": NOTICE,
                        "evidence_ids": [],
                    }
                    for c in CATEGORIES
                ],
                "exclusions": [],
                "excluded_pools": [],
                "included_services": list(CATEGORIES),
            }
            add(
                "institution_audit_periods",
                f"period-{year}",
                period_start=f"{year}-01-01",
                period_end=f"{year}-12-31",
                timezone="Asia/Kolkata",
                label=f"{year} sample",
                created_by=user_id,
                current_final_audit_id=audit_id,
            )
            audit = add(
                "institution_audits",
                f"audit-{year}",
                logical_audit_id=uid(f"period-{year}"),
                revision=1,
                period_start=f"{year}-01-01",
                period_end=f"{year}-12-31",
                timezone="Asia/Kolkata",
                label=f"{year} SAMPLE",
                method_version=mid,
                created_by=user_id,
                business_status="FINALIZED",
                processing_status="SUCCEEDED",
                input_hash=digest(audit_id),
                campus_ids=[cid],
                coverage=coverage,
                denominators={"student_count": 600},
                reviewed_snapshot_id=snapshot_id,
                current_result_id=uid(f"result-{year}"),
                latest_review_reason=NOTICE,
            )
            emissions = []
            details = [
                {
                    "grid_code": "SAMPLE",
                    "geography_code": "IN",
                    "meter_id": "sample-main",
                    "included_submeter_ids": [],
                    "total_load_kWh": None,
                },
                {
                    "fuel": "DIESEL",
                    "vehicle_class": "BUS",
                    "consumption_basis": "PURCHASED",
                    "stock_reconciliation": None,
                    "standard_conditions": None,
                    "density_source_id": None,
                },
                {
                    "fuel": "DIESEL",
                    "purpose": "GENERATOR",
                    "consumption_basis": "PURCHASED",
                    "stock_reconciliation": None,
                    "standard_conditions": None,
                    "density_source_id": None,
                    "consumed_cylinder_equivalents": None,
                    "verified_net_kg_per_cylinder": None,
                    "gas_components": None,
                },
                {
                    "material_code": "FOOD",
                    "pathway": "LANDFILL",
                    "treatment_operator": "SAMPLE",
                    "treatment_control": "OFFSITE_OTHER",
                    "mass_basis": "WET",
                    "feedstock_stream_id": "sample-food",
                    "included_energy_input_ids": [],
                    "included_transport_input_ids": [],
                    "gas_components": None,
                },
                {
                    "process": "SUPPLY",
                    "energy_source": "SUPPLIER",
                    "meter_id": "sample-water",
                    "supplier_boundary": "SAMPLE",
                    "utilized_water_kL": None,
                    "energy_intensity_source_id": None,
                },
            ]
            for (
                key,
                category,
                subcategory,
                _,
                _,
                _,
                unit,
                _,
                _,
            ), quantity, detail in zip(
                factor_configs, ("120000", "1800", "250", "4", "2000"), details
            ):
                emissions.append(
                    add(
                        "institution_emission_inputs",
                        f"emission-{year}-{key}",
                        audit_id=audit_id,
                        campus_id=cid,
                        category=category,
                        subcategory=subcategory,
                        source_id="sample-" + key,
                        quantity=quantity,
                        unit=unit,
                        activity_start=f"{year}-01-01",
                        activity_end=f"{year}-12-31",
                        source_type="ESTIMATED",
                        estimate_notes=NOTICE,
                        evidence_ids=[],
                        factor_id=factor_ids["SAMPLE_" + key.upper()],
                        details=detail,
                        created_by=user_id,
                    )
                )
            aux = deepcopy(emissions[0])
            aux.update(
                id=uid(f"solar-aux-{year}"),
                source_id="sample-solar-aux",
                quantity=numeric(Decimal(0)),
            )
            data["institution_emission_inputs"].append(aux)
            emissions.append(aux)
            mitigation = add(
                "institution_mitigation_inputs",
                f"solar-{year}",
                audit_id=audit_id,
                campus_id=cid,
                action="SOLAR",
                activity_start=f"{year}-01-01",
                activity_end=f"{year}-12-31",
                method_version=mid,
                source_type="ESTIMATED",
                estimate_notes=NOTICE,
                created_by=user_id,
                evidence_ids=[],
                baseline={
                    "displaced_self_kWh": "15000",
                    "eligible_export_kWh": "0",
                    "displaced_grid_factor_id": factor_ids["SAMPLE_AVOIDED_GRID"],
                    "export_factor_id": None,
                },
                project={
                    "generated_kWh": "15000",
                    "self_used_kWh": "15000",
                    "exported_kWh": "0",
                    "losses_kWh": "0",
                    "meter_refs": [],
                    "project_component_input_ids": [aux["id"]],
                },
                attributes={
                    "ownership": "OWNED",
                    "transferred_attributes": False,
                    "assessment_boundary": NOTICE,
                },
                overlap_refs=[],
            )
            link = add(
                "institution_audit_tree_links",
                f"tree-link-{year}",
                audit_id=audit_id,
                campus_id=cid,
                campus_tree_audit_id=stock_id,
                period_start=f"{year}-01-01",
                period_end=f"{year}-12-31",
                comparability_status=stock["comparability_status"],
                attribution_snapshot={
                    "attributed_removal_tCO2": stock["removal_tCO2"],
                    "attributed_release_tCO2": stock["release_tCO2"],
                    "notice": NOTICE,
                },
                created_by=user_id,
            )
            frozen = {
                "audit": audit,
                "emissions": emissions,
                "mitigations": [mitigation],
                "tree_links": [
                    {**link, **link["attribution_snapshot"], "assessment": stock}
                ],
                "factors": factors,
                "parameters": {},
                "method": {"id": mid, "config": method_config},
                "input_hash": audit["input_hash"],
            }
            result, errors = calculate_inventory(frozen, snapshot_id=snapshot_id)
            if errors:
                raise ValueError("Invalid sample inventory: " + str(errors))
            result["id"] = uid(f"result-{year}")
            job(f"calculation-job-{year}", "institution_calculation", audit_id)
            add(
                "institution_calculation_snapshots",
                f"snapshot-{year}",
                audit_id=audit_id,
                source_input_revision=1,
                input_hash=audit["input_hash"],
                result_hash=result["result_hash"],
                processing_job_id=uid(f"calculation-job-{year}"),
                created_as_preview=False,
                frozen_input=frozen,
                result=result,
                quality={"flags": ["SYNTHETIC_SAMPLE"]},
                missing_data=[],
            )
            add(
                "institution_audit_results",
                f"result-{year}",
                audit_id=audit_id,
                snapshot_id=snapshot_id,
                result_revision=1,
                input_hash=audit["input_hash"],
                result_hash=result["result_hash"],
                result=result,
            )
            for index, calculation in enumerate(result["calculation_lines"]):
                calculation["id"] = uid(f"line-{year}-{index}")
                from repositories.institution.base import columns

                values = {
                    k: v
                    for k, v in calculation.items()
                    if k in columns("institution_calculation_lines") and k != "id"
                }
                values.update(
                    input_type="MITIGATION"
                    if calculation["formula_key"].startswith("MIT-")
                    else "EMISSION",
                    result_unit=calculation["contribution_unit"],
                )
                add(
                    "institution_calculation_lines",
                    f"line-{year}-{index}",
                    audit_id=audit_id,
                    snapshot_id=snapshot_id,
                    **values,
                )
            for kind in ("PDF", "CSV", "JSON"):
                export_id, artifact_id = (
                    uid(f"export-{year}-{kind}"),
                    uid(f"artifact-{year}-{kind}"),
                )
                manifest = {
                    "frozen_input": frozen,
                    "result": result,
                    "draft_watermark": False,
                    "data_origin": "SYNTHETIC",
                    "demo": True,
                    "warnings": [NOTICE],
                }
                content, mime = render(manifest, kind)
                obj = SampleObject(
                    settings.REPORT_STORAGE_BUCKET,
                    f"demo-samples/{iid}/{year}.{kind.lower()}",
                    content,
                    mime,
                )
                sample.objects.append(obj)
                job(f"export-job-{year}-{kind}", "report_export", export_id)
                add(
                    "institution_artifacts",
                    f"artifact-{year}-{kind}",
                    parent_type="EXPORT",
                    parent_id=export_id,
                    producer_job_id=uid(f"export-job-{year}-{kind}"),
                    snapshot_id=snapshot_id,
                    private_bucket=obj.bucket,
                    object_key=obj.object_key,
                    mime_type=mime,
                    status="READY",
                    draft=False,
                    byte_hash=obj.sha256,
                    size_bytes=obj.size_bytes,
                )
                add(
                    "institution_exports",
                    f"export-{year}-{kind}",
                    audit_id=audit_id,
                    snapshot_id=snapshot_id,
                    result_hash=result["result_hash"],
                    format=kind,
                    job_id=uid(f"export-job-{year}-{kind}"),
                    status="SUCCEEDED",
                    artifact_id=artifact_id,
                    draft_watermark=False,
                    include_advice=False,
                    advice_query_ids=[],
                    advice_hashes={},
                    created_by=user_id,
                )
    sample.tables = [
        CheckpointTable(
            table=table, rows=normalize(rows), row_hashes=[digest(r) for r in rows]
        )
        for table, rows in data.items()
    ]
    return sample


async def ensure_sample_globals(repo, sample):
    """Create only namespaced SYNTHETIC sources; preserve every existing source."""
    from sqlalchemy import text

    await repo.conn.execute(
        text(
            "SELECT pg_advisory_xact_lock(hashtextextended('tt:sample-demo-sources',0))"
        )
    )
    method = sample.globals[0][1]
    existing = await repo.get("carbon_methodologies", method["id"], required=False)
    if existing:
        if (
            existing["data_origin"] != "SYNTHETIC"
            or existing["artifact_hash"] != method["artifact_hash"]
            or existing["status"] != "APPROVED"
        ):
            fail(
                409,
                "DEMO_SAMPLE_CONFLICT",
                "A sample-source identifier conflicts with existing data.",
            )
        return
    await repo.insert("carbon_methodologies", {**method, "status": "DRAFT"})
    for table, value in sample.globals[1:]:
        await repo.insert(table, value)
    await repo.update("carbon_methodologies", method["id"], {"status": "APPROVED"})


async def prepare_sample(repo, sample):
    """Measure sample geometry with the same PostGIS path used by the app."""
    from services.shared.sampling_service import measure

    for bundle in sample.tables:
        for value in bundle.rows:
            field = {
                "campus_boundaries": "area_ha",
                "campus_vegetation_masks": "eligible_area_ha",
                "campus_sampling_plots": "measured_area_ha",
            }.get(bundle.table)
            if field:
                geo_key = (
                    "eligible_geometry"
                    if bundle.table == "campus_vegetation_masks"
                    else "geometry"
                )
                _, area = await measure(repo, value[geo_key])
                value[field] = numeric(Decimal(str(area)))
        bundle.row_hashes = [digest(value) for value in bundle.rows]


async def seed_persistent_once(repo, sample):
    """Account nine is insert-only: never clear or replace its existing work."""
    from sqlalchemy import text

    from services.shared.evidence_service import write_bytes

    if await repo.find("institutions", {"created_by": sample.target_user_id}):
        return False
    if await repo.find("institution_users", {"user_id": sample.target_user_id}):
        fail(
            409,
            "DEMO_SAMPLE_CONFLICT",
            "Persistent demo already has membership; its existing data is preserved.",
        )
    for bundle in sample.tables:
        if bundle.table == "institutions":
            for value in bundle.rows:
                if await repo.get("institutions", value["id"], required=False):
                    fail(
                        409,
                        "DEMO_SAMPLE_CONFLICT",
                        "A sample identifier is already in use.",
                    )
    await repo.conn.execute(text("SET CONSTRAINTS ALL DEFERRED"))
    for obj in sample.objects:
        await write_bytes(obj.bucket, obj.object_key, obj.data, obj.mime_type)
    priority = {
        "institutions": 0,
        "institution_registrations": 1,
        "institution_users": 2,
        "campuses": 3,
        "campus_tree_surveys": 4,
        "institution_audit_periods": 5,
        "institution_audits": 6,
    }
    final_states = []
    for bundle in sorted(sample.tables, key=lambda item: priority.get(item.table, 7)):
        for value in bundle.rows:
            staged = dict(value)
            field = {
                "campus_tree_surveys": "status",
                "institution_audits": "business_status",
            }.get(bundle.table)
            if field:
                final_states.append((bundle.table, value["id"], field, value[field]))
                staged[field] = "DRAFT"
            await repo.insert(bundle.table, staged)
    for table, rid, field, state in final_states:
        await repo.update(table, rid, {field: state})
    await repo.conn.execute(
        text(
            "UPDATE users SET institution_onboarding_status='COMPLETE',institution_onboarding_completed_at=COALESCE(institution_onboarding_completed_at,now()) WHERE id=:uid AND account_domain='INSTITUTION'"
        ),
        {"uid": sample.target_user_id},
    )
    await repo.conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    return True
