"""Reviewed source/config validation and exact purpose/unit/date factor selection."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.institution_errors import fail
from models.institution.contracts import MODELS
from models.institution.common import digest, normalize
from services.institution.base import evidence

REGISTRIES = {"FACTOR": "emission_factors", "METHOD": "carbon_methodologies", "MODEL": "model_registry", "KNOWLEDGE": "knowledge_documents"}
CONFIGS = {"FACTOR": "FactorConfig", "METHOD": "MethodConfig", "MODEL": "ModelConfig", "KNOWLEDGE": "KnowledgeConfig"}
FORMULAS = set("EM-GRID-01 EM-TRANSPORT-01 EM-STATIONARY-01 EM-WASTE-01 EM-PUMP-01 EM-WATER-01 EM-GAS-01 MIT-SOLAR-01 MIT-RAIN-01 MIT-BIOGAS-01 MIT-COMPOST-01 MIT-TOTAL-01 INVENTORY-TOTAL-01 BALANCE-01 SCENARIO-BALANCE-01 CHAVE_2014_HEIGHT CIRCUMFERENCE_TO_DBH".split())


def compile_schema(schema):
    def check(node):
        if isinstance(node, dict):
            if "$dynamicRef" in node or "$recursiveRef" in node:
                fail(422,"UNSAFE_SCHEMA","Dynamic or recursive JSON Schema references are unsupported.")
            if "$ref" in node and not node["$ref"].startswith("#/"):
                fail(422, "UNSAFE_SCHEMA", "Remote JSON Schema references are forbidden.")
            if node.get("type") == "object" and node.get("additionalProperties") is not False:
                fail(422, "INVALID_SCHEMA", "Every declared object must reject unknown properties.")
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for item in node:
                check(item)
    check(schema)
    def check_references(node, ancestry):
        if isinstance(node,dict):
            ref = node.get("$ref")
            if ref:
                if ref in ancestry:
                    fail(422,"UNSAFE_SCHEMA","Recursive schema references are unsupported for bounded activity forms.")
                target = schema
                try:
                    for key in ref[2:].split("/"):
                        target = target[key.replace("~1","/").replace("~0","~")]
                except (KeyError,TypeError):
                    fail(422,"INVALID_SCHEMA","Local schema reference does not resolve.")
                check_references(target,ancestry|{ref})
            for value in node.values():
                check_references(value,ancestry)
        elif isinstance(node,list):
            for value in node:
                check_references(value,ancestry)
    check_references(schema,set())
    try:
        Draft202012Validator.check_schema(schema)
    except Exception:
        fail(422, "INVALID_SCHEMA", "The published input schema is invalid.")
    return Draft202012Validator(schema)


def ranking_rules(config):
    classes = ["DATA_REQUIRED", "SUPPORTED_ACTION", "SITE_CHECK_REQUIRED"]
    if config.get("mode") != "RULES_V1" or config.get("class_order") != classes or config.get("within_class") != "CATEGORY_SHARE_DESC_THEN_ACTION_ID_ASC" or config.get("unknown_share") != "LAST":
        fail(422, "UNSUPPORTED_RANKING", "Select the implemented reviewed RULES_V1 ordering.")
    return {name: index for index, name in enumerate(config["class_order"])}


def validate_config(kind, config, *, draft=False):
    schema = MODELS[CONFIGS[kind]].model_json_schema()
    if draft:
        def optional(node):
            if isinstance(node, dict):
                node.pop("required", None)
                for value in node.values():
                    optional(value)
            elif isinstance(node, list):
                for value in node:
                    optional(value)
        optional(schema)
        errors = list(Draft202012Validator(schema).iter_errors(config))
        if errors:
            fail(422, "INVALID_REGISTRY_CONFIG", "Draft configuration contains unsupported fields or types.")
        return config
    try:
        config = MODELS[CONFIGS[kind]].model_validate(config).model_dump(exclude_unset=True)
    except ValidationError:
        fail(422, "METHOD_NOT_CONFIGURED", "Complete every required source/configuration field before publication.")
    if kind == "FACTOR" and Decimal(config["value"]) < 0:
        fail(422, "INVALID_FACTOR_VALUE", "Emission factor values cannot be negative.")
    if kind == "METHOD":
        if set(config["formula_keys"]) - FORMULAS:
            fail(422, "UNSUPPORTED_FORMULA", "The method requests an unimplemented formula.")
        for schema in [config["required_inputs"], *config["activity_schemas"].values()]:
            compile_schema(schema)
        ids = [p["id"] for p in config["parameters"]]
        if len(ids) != len(set(ids)):
            fail(422, "DUPLICATE_PARAMETER", "Method parameter IDs must be unique.")
        for parameter in config["parameters"]:
            value, unit, parameter_kind = parameter["value"], parameter["unit"], parameter["kind"]
            if not unit.strip():
                fail(422, "INVALID_PARAMETER_UNIT", "Every reviewed parameter requires its explicit source unit.")
            if value is not None:
                value = Decimal(value)
                if parameter_kind in {"WOOD_DENSITY", "LHV", "GWP"} and value <= 0 or parameter_kind in {"EFFICIENCY", "CARBON_FRACTION"} and not 0 < value <= 1 or parameter_kind == "ENERGY_INTENSITY" and value < 0:
                    fail(422, "INVALID_PARAMETER_VALUE", "Source parameters must satisfy their physical range.")
        quality = config["quality_policy"]
        if quality["temporal"]["revisit_matching_rule"] != "STABLE_TREE_ID_AND_PLOT_SET_V1" or quality["temporal"]["max_window_days"] < 0 or quality["temporal"]["seasonal_tolerance_days"] < 0:
            fail(422, "UNSUPPORTED_TEMPORAL_POLICY", "Commission the implemented stable-tree/plot matching rule with nonnegative temporal bounds.")
        if not quality["eligibility"]["species_codes"] or set(quality["eligibility"]["pools"]) != set(config["pools"]) or not set(quality["eligibility"]["allometry_ids"]) <= set(ids):
            fail(422, "INVALID_QUALITY_POLICY", "Survey eligibility must match the method's pools, species and exact equation IDs.")
        if Decimal(quality["gps"]["max_accuracy_m"]) <= 0 or Decimal(quality["gps"]["plot_tolerance_m"]) < 0 or Decimal(quality["sampling"]["plot_area_ha"]) <= 0 or Decimal(quality["sampling"]["min_spacing_m"]) < 0:
            fail(422, "INVALID_QUALITY_POLICY", "GPS and sampling support values must be physically valid.")
        confidence = quality["species"]["minimum_classifier_confidence"]
        if Decimal(quality["gps"]["boundary_tolerance_m"]) < 0 or Decimal(quality["dimensions"]["reference_height_m"]) <= 0 or quality["evidence"]["minimum_photos"] < 0 or confidence is not None and not 0 <= Decimal(confidence) <= 1:
            fail(422, "INVALID_QUALITY_POLICY", "Boundary tolerance, reference height, evidence count and classifier confidence must satisfy their physical ranges.")
        for dimension in ("dbh", "height"):
            if not 0 < Decimal(quality["dimensions"][dimension + "_min_" + ("cm" if dimension == "dbh" else "m")]) <= Decimal(quality["dimensions"][dimension + "_max_" + ("cm" if dimension == "dbh" else "m")]):
                fail(422, "INVALID_QUALITY_POLICY", "Reviewed dimension ranges require positive ordered limits.")
        if not 0 < Decimal(quality["validation"]["minimum_coverage_ratio"]) <= 1 or Decimal(quality["validation"]["minimum_eligible_area_ha"]) <= 0 or not quality["sampling"]["required_plots_by_stratum"] or any(value <= 0 for value in quality["sampling"]["required_plots_by_stratum"].values()):
            fail(422, "INVALID_QUALITY_POLICY", "Coverage, eligible area and stratum plot support must be positive.")
        if config["ranking_mode"] != config["ranking_config"]["mode"]:
            fail(422, "UNSUPPORTED_RANKING", "Method and ranking configuration must name the same mode.")
        ranking_rules(config["ranking_config"])
        policy = config["attribution_policy"]
        if (policy["supported_gain_rules"] or policy["supported_loss_rules"]) and not policy["validation_artifact_id"]:
            fail(422, "ATTRIBUTION_VALIDATION_REQUIRED", "Atmospheric attribution requires actual supporting validation evidence.")
        compile_schema(config["uncertainty_policy"]["required_inputs"])
        compile_schema(policy["period_matching_schema"])
        references = set(config["uncertainty_policy"]["parameter_ids"] + policy["parameter_ids"])
        for rule in policy["supported_gain_rules"] + policy["supported_loss_rules"]:
            compile_schema(rule["required_evidence_schema"])
            references.update(rule["parameter_ids"])
        if not references <= set(ids):
            fail(422, "UNKNOWN_METHOD_PARAMETER", "Policy rules may reference only this exact method's reviewed parameters.")
    if kind == "MODEL":
        for field in ("training_domain", "validation_domain", "applicability_schema"):
            compile_schema(config[field])
        feature = config["feature_schema"]
        count = len(feature["ordered_names"])
        if not count or any(len(feature[key]) != count for key in ("units", "native_resolution_m")) or len(set(feature["ordered_names"])) != count:
            fail(422, "INVALID_FEATURE_SCHEMA", "Feature names, units and native resolutions must align.")
        if Decimal(config["spatial_resolution_m"]) <= 0 or any(Decimal(value) <= 0 for value in feature["native_resolution_m"]) or config["inference_limits"]["max_tile_pixels"] <= 0:
            fail(422, "INVALID_FEATURE_SCHEMA", "Model pixel support and inference bounds must be positive.")
        if not config["artifact_id"] or not config["artifact_sha256"] or not config["validation_artifact_id"] or not config["evaluation_metrics"]:
            fail(422, "MODEL_NOT_VALIDATED", "An actual model artifact and independent validation are required.")
    return config


async def approved(repo, table, version, *, kind=None):
    row = await repo.get(table, version)
    if row["status"] not in {"APPROVED", "ACTIVE"}:
        fail(422, "METHOD_NOT_CONFIGURED", "Select an approved applicable version.")
    from app.config import settings
    if (settings.DEPLOYMENT_ENV == "production" or settings.ENVIRONMENT == "production") and row["data_origin"] != "LIVE":
        fail(422, "SYNTHETIC_SOURCE_FORBIDDEN", "Production cannot use synthetic sources.")
    return row


def process_for(line):
    details = line["details"]
    return details.get("process") or details.get("pathway") or details.get("purpose") or details.get("vehicle_class") or line["subcategory"]


def compatible(factor, line, purpose="INVENTORY"):
    config, details = factor["config"], line["details"]
    if factor["status"] not in {"APPROVED", "ACTIVE"} or config["purpose"] != purpose:
        return False
    if config["category"] != line["category"] or config["subcategory"] != line["subcategory"]:
        return False
    if config["process"] != process_for(line):
        return False
    if config.get("fuel") and config["fuel"] != details.get("fuel"):
        return False
    if config.get("material") and config["material"] != details.get("material_code"):
        return False
    if config.get("mass_basis") and config["mass_basis"] != details.get("mass_basis"):
        return False
    if config.get("standard_conditions") != details.get("standard_conditions") and line["unit"] == "std_m3":
        return False
    start, end = str(line["activity_start"]), str(line["activity_end"])
    if factor.get("valid_from") and str(factor["valid_from"]) > start:
        return False
    if factor.get("valid_to") and str(factor["valid_to"]) < end:
        return False
    geography = details.get("geography_code")
    if geography and geography not in factor["geography_codes"]:
        return False
    try:
        convert(Decimal("1"), line["unit"], config["denominator_unit"], category=line["category"])
    except ValueError:
        return False
    if line["category"] == "ELECTRICITY" and config["scope"] != "SCOPE_2":
        return False
    if line["category"] in {"TRANSPORT", "STATIONARY_FUEL"} and config["scope"] != "SCOPE_1":
        return False
    if line["category"] == "WASTE" and config["scope"] != ("SCOPE_1" if details["treatment_control"] == "ONSITE_CONTROLLED" else "SCOPE_3"):
        return False
    return True


def convert(quantity, source, target, *, category=None):
    if source == target:
        return quantity
    permitted = {("kg", "t"): Decimal("0.001"), ("t", "kg"): Decimal("1000"), ("m3", "kL"): Decimal(1), ("kL", "m3"): Decimal(1)}
    if category == "WATER":
        permitted.update({("L", "m3"): Decimal("0.001"), ("L", "kL"): Decimal("0.001")})
    multiplier = permitted.get((source, target))
    if multiplier is None:
        raise ValueError("Unsupported unit conversion.")
    return quantity * multiplier


async def resolve_factor(repo, method, line):
    allowed = {str(fid) for mapping in method["config"]["factor_mappings"] if mapping["category"] == line["category"] and mapping["subcategory"] == line["subcategory"] and mapping["process"] == process_for(line) for fid in mapping["allowed_factor_ids"]}
    selected = line.get("factor_id")
    candidates = []
    for fid in sorted(allowed):
        row = await repo.get("emission_factors", fid)
        if compatible(row, line):
            candidates.append(row)
    if selected:
        candidates = [row for row in candidates if str(row["id"]) == selected]
        if not candidates:
            fail(422, "INCOMPATIBLE_FACTOR", "The chosen factor does not match this activity, method, purpose or full date range.")
    if len(candidates) > 1:
        fail(422, "FACTOR_SELECTION_REQUIRED", "Select the exact approved factor version.")
    return str(candidates[0]["id"]) if candidates else None
