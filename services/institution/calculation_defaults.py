"""Implemented formula structure; real site limits and factors stay in Supabase."""

from copy import deepcopy

from models.institution.contracts import MODELS, DETAILS
from services.institution.factor_service import FORMULAS


EMPTY_OBJECT = {"type": "object", "properties": {}, "additionalProperties": False}


def defaults():
    from services.fusion_engine import SPECIES_WOOD_DENSITY

    applicability = {
        "type": "object",
        "properties": {
            "species_code": {"type": "string"},
            "dbh_cm": {"type": "string"},
            "height_m": {"type": "string"},
            "domain": {"const": "INSTITUTION"},
        },
        "additionalProperties": False,
    }
    parameters = [
        {
            "logical_key": "CHAVE_2014_HEIGHT",
            "kind": "ALLOMETRY",
            "value": None,
            "unit": "kg",
            "formula_key": "CHAVE_2014_HEIGHT",
            "required_inputs": deepcopy(EMPTY_OBJECT),
            "applicability": deepcopy(applicability),
            "uncertainty": None,
        }
    ]
    for species, rho in SPECIES_WOOD_DENSITY.items():
        schema = deepcopy(applicability)
        schema["properties"]["species_code"] = {"const": species}
        parameters.append(
            {
                "logical_key": "WOOD_DENSITY:" + species,
                "kind": "WOOD_DENSITY",
                "value": str(rho),
                "unit": "g/cm3",
                "formula_key": None,
                "required_inputs": deepcopy(EMPTY_OBJECT),
                "applicability": schema,
                "uncertainty": None,
            }
        )
    schemas = {
        category: MODELS[category + "Details"].model_json_schema()
        for category in DETAILS
    }
    for action in ("SOLAR", "RAINWATER", "BIOGAS", "COMPOSTING"):
        # The public API already enforces the complete typed action contract.
        fields = MODELS["MitigationLine"].model_fields
        schemas[action] = {
            "type": "object",
            "properties": {key: {} for key in fields},
            "additionalProperties": False,
        }
    return {
        "parameters": parameters,
        "formula_keys": sorted(FORMULAS),
        "coverage_categories": list(DETAILS),
        "included_scopes": ["SCOPE_1", "SCOPE_2", "SCOPE_3"],
        "pools": ["LIVE_ABOVE_GROUND"],
        "required_inputs": deepcopy(EMPTY_OBJECT),
        "activity_schemas": schemas,
        "unit_conversions": [],
        "factor_mappings": [],
        "uncertainty_policy": {
            "schema_version": 1,
            "method_key": "UNKNOWN",
            "parameter_ids": [],
            "required_inputs": deepcopy(EMPTY_OBJECT),
            "confidence_level": None,
            "dependence_assumptions": [],
            "unknown_handling": "REPORT_UNKNOWN",
            "validation_artifact_id": None,
        },
        "attribution_policy": {
            "schema_version": 1,
            "supported_gain_rules": [],
            "supported_loss_rules": [],
            "period_matching_schema": deepcopy(EMPTY_OBJECT),
            "parameter_ids": [],
            "overlap_rules": [],
            "validation_artifact_id": None,
        },
        "epsilon_tCO2e": "0",
        "epsilon_rationale": "Exact zero threshold unless the administrator supplies a documented tolerance.",
        "overlap_rules": [],
        "ranking_mode": "RULES_V1",
        "ranking_config": {
            "mode": "RULES_V1",
            "class_order": ["DATA_REQUIRED", "SUPPORTED_ACTION", "SITE_CHECK_REQUIRED"],
            "within_class": "CATEGORY_SHARE_DESC_THEN_ACTION_ID_ASC",
            "unknown_share": "LAST",
        },
    }


def template():
    from services.fusion_engine import SPECIES_WOOD_DENSITY

    # NULL denotes a required site input, never a guessed scientific constant.
    return {
        "epsilon_tCO2e": "0",
        "quality_policy": {
            "eligibility": {
                "pools": ["LIVE_ABOVE_GROUND"],
                "species_codes": list(SPECIES_WOOD_DENSITY),
                "allometry_ids": ["CHAVE_2014_HEIGHT"],
            },
            "sampling": {
                "design": "STRATIFIED_REVISITABLE",
                "seed": 0,
                "strata_rule": deepcopy(EMPTY_OBJECT),
                "plot_area_ha": None,
                "min_spacing_m": None,
                "edge_rule": "REQUIRE_FULL_PLOT",
                "required_plots_by_stratum": {},
                "inclusion_weight_rule": "REVIEWED_FIXED_PLOT_WEIGHTS_V1",
            },
            "dimensions": {
                "dbh_min_cm": None,
                "dbh_max_cm": None,
                "height_min_m": None,
                "height_max_m": None,
                "reference_height_m": None,
                "multistem_rule": "UNSUPPORTED",
                "irregular_stem_rule": "REQUIRE_EXPERT_REVIEW",
            },
            "gps": {
                "max_accuracy_m": None,
                "boundary_tolerance_m": None,
                "plot_tolerance_m": None,
            },
            "evidence": {
                "required_types": ["PHOTO"],
                "minimum_photos": None,
                "override_policy": "DISALLOW",
            },
            "species": {
                "selection_policy": "MANUAL_CONFIRMED",
                "minimum_classifier_confidence": None,
                "missing_species_rule": "REQUIRE_CORRECTION",
            },
            "capture_flags": ["MISSING_HEIGHT", "MISSING_GPS", "UNKNOWN_SPECIES"],
            "temporal": {
                "max_window_days": None,
                "seasonal_tolerance_days": None,
                "revisit_matching_rule": "STABLE_TREE_ID_AND_PLOT_SET_V1",
            },
            "validation": {
                "minimum_eligible_area_ha": None,
                "minimum_coverage_ratio": None,
                "spatial_holdout_rule": "NOT_QUANTIFIED_BY_SHARED_FARMER_IMPLEMENTATION",
                "maximum_bias_rule": "NOT_QUANTIFIED",
                "validation_artifact_id": None,
            },
            "uncertainty": {
                "procedure": "UNKNOWN",
                "dependence_assumptions": "No confidence bounds are established by the shared farmer implementation.",
                "validation_artifact_id": None,
            },
        },
        "campus_processing": {
            "mask_engine": "REVIEWED_GEE_MASK_ASSET_V1",
            "mask_asset_id": None,
            "mask_asset_update_time": None,
            "mask_tree_value": None,
            "mask_source_date": None,
            "mask_native_resolution_m": None,
            "mask_validation_evidence_id": None,
            "strata": [
                {
                    "name": None,
                    "geometry": None,
                    "evidence_id": None,
                    "inclusion_probability": None,
                    "design_weight": None,
                }
            ],
        },
    }


def normal_settings():
    """Practical starter capture limits; these do not assert model validation."""
    config = template()
    config.update(
        epsilon_tCO2e="0.01",
        epsilon_rationale="Starter balance tolerance of 0.01 tCO2e (10 kg); administrative tolerance, not measurement uncertainty.",
        campus_processing=None,
    )
    quality = config["quality_policy"]
    quality["sampling"].update(
        plot_area_ha="0.01",
        min_spacing_m="0",
        required_plots_by_stratum={"TREE_AREA": 3},
    )
    quality["dimensions"].update(
        dbh_min_cm="5",
        dbh_max_cm="300",
        height_min_m="1",
        height_max_m="60",
        reference_height_m="1.3",
    )
    quality["gps"].update(
        max_accuracy_m="20", boundary_tolerance_m="5", plot_tolerance_m="5"
    )
    quality["evidence"]["minimum_photos"] = 1
    quality["temporal"].update(max_window_days=366, seasonal_tolerance_days=45)
    quality["validation"].update(
        minimum_eligible_area_ha="0.01", minimum_coverage_ratio="0.5"
    )
    return config


def merge(config):
    result = defaults()
    parameters = {
        parameter["logical_key"]: parameter for parameter in result["parameters"]
    }
    for parameter in config.get("parameters", []):
        parameters[parameter["logical_key"]] = deepcopy(parameter)
    result.update(deepcopy(config))
    result["parameters"] = list(parameters.values())
    return result
