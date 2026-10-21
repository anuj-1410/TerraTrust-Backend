"""Evaluate reviewed applicability against observed campus facts only."""
from datetime import date
from app.institution_errors import fail
from models.institution.common import normalize
from services.institution.factor_service import compile_schema


def candidate(config, facts):
    """Check known setup facts; dates/species remain subject to final admission.

    Preparation has no selected observation window yet. Required fields must
    not make every model disappear, and unrelated invented facts must not be
    sent to schemas that explicitly forbid additional properties.
    """
    for name in ("training_domain", "validation_domain", "applicability_schema"):
        schema = config[name]
        validator = compile_schema(schema)
        for key, value in facts.items():
            rule = schema.get("properties", {}).get(key)
            if rule is not None and list(validator.descend(value, rule)):
                return False
    if config["mode"] == "APPROVED_CAMPUS_CALIBRATION":
        return config["validation_domain"].get("properties", {}).get("campus_id", {}).get("const") == facts["campus_id"]
    return config["mode"] == "PRETRAINED_DOMAIN"


def validate(frozen):
    if frozen["model"]["config"].get("engine") == "FARMER_GEE_GBT_V3_1":
        from services.institution.farmer_biomass import validate as validate_shared
        return validate_shared(frozen)
    config, request = frozen["model"]["config"], frozen["request"]
    address = frozen["campus"]["address"]
    temporal = frozen["survey"].get("policy", {}).get("config", {}).get("temporal")
    start, end, assessed = (date.fromisoformat(request[key]) for key in ("observation_start", "observation_end", "assessment_date"))
    if start > end or end > assessed:
        fail(422, "INVALID_OBSERVATION_WINDOW", "Observation dates must be ordered and cannot follow the stock assessment date.")
    if temporal is not None and (end - start).days > temporal["max_window_days"]:
        fail(422, "OBSERVATION_WINDOW_TOO_LARGE", "The observation window exceeds the approved survey policy.")
    facts = {"domain": "INSTITUTION", "campus_id": str(frozen["campus"]["id"]), "country": address["country"], "state": address["state"], "species_codes": sorted({row["raw_input"]["species_code"] for row in frozen["survey"]["measurements"]}), "pools": frozen["method"]["config"]["pools"], "eligible_area_ha": normalize(frozen["mask"]["eligible_area_ha"]), "assessment_month": int(request["assessment_date"][5:7]), "observation_start": request["observation_start"], "observation_end": request["observation_end"]}
    for field in ("training_domain", "validation_domain", "applicability_schema"):
        if list(compile_schema(config[field]).iter_errors(facts)):
            fail(422, "MODEL_OUTSIDE_VALIDATED_DOMAIN", "Observed campus facts are outside the reviewed training, validation or applicability domain.")
    if config["mode"] == "APPROVED_CAMPUS_CALIBRATION":
        campus_rule = config["validation_domain"].get("properties", {}).get("campus_id", {})
        if campus_rule.get("const") != facts["campus_id"]:
            fail(422, "CAMPUS_CALIBRATION_REQUIRED", "A campus-calibrated artifact must bind independent validation to this exact campus.")
    rules = {"window_rule": "REQUEST_INCLUSIVE_DATES_V1", "mask_rule": "APPROVED_ELIGIBLE_MASK_V1", "fractional_pixel_rule": "POSTGIS_GEODESIC_PIXEL_INTERSECTION_V1"}
    if any(config["feature_schema"][key] != value for key, value in rules.items()):
        fail(422, "UNSUPPORTED_FEATURE_RULE", "Commission the implemented temporal, eligible-mask and fractional-pixel rules.")
    return facts
