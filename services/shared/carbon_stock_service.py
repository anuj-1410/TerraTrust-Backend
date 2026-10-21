"""Standing stock, compatible signed change and explicit atmospheric attribution."""
from datetime import date, timedelta
from decimal import Decimal, localcontext

from models.institution.common import normalize


def same_season(baseline, frozen):
    temporal = frozen.get("survey", {}).get("policy", {}).get("config", {}).get("temporal", {})
    before = baseline.get("provenance", {})
    window = before.get("observation_window", {})
    survey = frozen.get("survey", {})
    if temporal.get("revisit_matching_rule") != "STABLE_TREE_ID_AND_PLOT_SET_V1" or not window:
        return False
    if before.get("plot_set_id") != str(survey.get("survey", {}).get("plot_set_id")) or before.get("quality_policy_hash") != survey.get("policy", {}).get("artifact_hash"):
        return False
    for key in ("observation_start", "observation_end"):
        a, b = (date.fromisoformat(value).replace(year=2000).timetuple().tm_yday for value in (window[key], frozen["request"][key]))
        distance = abs(a - b)
        if min(distance, 366 - distance) > temporal["seasonal_tolerance_days"]:
            return False
    return True


def stock(agb, fraction, frozen, *, co2_multiplier=None):
    with localcontext() as ctx:
        ctx.prec = 50
        if not Decimal(0) < fraction <= 1:
            raise ValueError("Reviewed carbon fraction must be in (0,1].")
        carbon = agb * fraction
        multiplier = co2_multiplier if co2_multiplier is not None else Decimal(44) / 12
        co2 = carbon * multiplier
        result = {"agb_t": agb, "stock_tC": carbon, "stock_co2_t": co2, "signed_change_tCO2": None, "stock_gain_tCO2": None, "stock_loss_tCO2": None, "removal_tCO2": None, "release_tCO2": None, "comparison_start": None, "comparison_end": None, "comparability_status": "BASELINE_ONLY", "attribution_policy_version": None, "attribution_reason": "First accepted stock assessment; annual removal is unavailable.", "attribution_sources": [], "quality_flags": ["UNKNOWN_UNCERTAINTY"], "uncertainty": {"type": "UNKNOWN", "lower": None, "upper": None, "confidence_level": None, "method_version": None, "dependence_assumptions": None, "sources": []}}
        baseline = frozen.get("baseline")
        if not baseline:
            result["quality_flags"].append("BASELINE_ONLY")
            return normalize(result)
        request = frozen["request"]
        compatible = all(str(baseline[key]) == str(request[key]) for key in ("boundary_id", "mask_id", "method_version", "model_version", "feature_schema_version")) and baseline["stock_tC"] is not None and baseline["assessment_date"] < request["assessment_date"] and same_season(baseline, frozen)
        if not compatible:
            result.update(comparability_status="NONCOMPARABLE", attribution_reason="Boundary, mask, pools, model or method differs; reviewed consistent re-estimation is required.")
            result["quality_flags"].append("NONCOMPARABLE")
            return normalize(result)
        change = (carbon - Decimal(str(baseline["stock_tC"]))) * multiplier
        result.update(signed_change_tCO2=change, stock_gain_tCO2=max(change, Decimal(0)), stock_loss_tCO2=max(-change, Decimal(0)), comparison_start=(date.fromisoformat(baseline["assessment_date"]) + timedelta(days=1)).isoformat(), comparison_end=request["assessment_date"], comparability_status="COMPARABLE", attribution_reason="Comparable live-pool change; atmospheric fate is unproven.")
        policy = frozen["method"]["config"]["attribution_policy"]
        from jsonschema import Draft202012Validator
        facts = {"events": frozen.get("events", []), "change_tCO2": normalize(change), "baseline_date": baseline["assessment_date"], "assessment_date": request["assessment_date"], "same_boundary": True, "same_model": True}
        # These allowlisted rules require independently reviewed evidence schemas.
        # Any unresolved mortality/removal/import event prevents automatic assignment.
        old_trees = set(baseline.get("provenance", {}).get("tree_ids", []))
        current_trees = {str(tree["id"]) for tree in frozen.get("survey", {}).get("trees", [])}
        if not frozen.get("events") and old_trees and old_trees == current_trees:
            for rule in policy["supported_gain_rules"]:
                if change >= 0 and rule["formula_key"] == "VERIFIED_GROWTH_NO_TRANSFERS_V1" and not list(Draft202012Validator(rule["required_evidence_schema"]).iter_errors(facts)) and set(rule["parameter_ids"]) <= set(request.get("attribution_parameter_ids", [])):
                    result.update(removal_tCO2=change, attribution_policy_version=frozen["method"]["id"], attribution_reason="Gain assigned under the independently reviewed no-transfer growth rule.", attribution_sources=rule["parameter_ids"])
            for rule in policy["supported_loss_rules"]:
                if change >= 0 and rule["formula_key"] == "VERIFIED_NO_STOCK_LOSS_V1" and not list(Draft202012Validator(rule["required_evidence_schema"]).iter_errors(facts)):
                    result["release_tCO2"] = Decimal(0)
        return normalize(result)
