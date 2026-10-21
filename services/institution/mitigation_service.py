"""Matched baseline/project impact, kept separate from actual inventory."""
from decimal import Decimal, localcontext
from uuid import uuid4

from models.institution.common import normalize
from services.institution.factor_service import convert
from services.institution.accounting_service import factor_snapshot

FORMULAS = {"SOLAR": "MIT-SOLAR-01", "RAINWATER": "MIT-RAIN-01", "BIOGAS": "MIT-BIOGAS-01", "COMPOSTING": "MIT-COMPOST-01"}


class Unquantified(ValueError):
    pass


def number(value):
    if value is None:
        raise Unquantified("A required baseline/project term is unknown.")
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("Activity cannot be negative or nonfinite.")
    return result


def parameter(parameters, pid, kind, units=None):
    row = parameters.get(str(pid))
    if not row or row["kind"] != kind or row["value"] is None:
        raise Unquantified("A reviewed source parameter is required.")
    if units is not None and row.get("unit") not in units:
        raise Unquantified("The reviewed parameter unit does not match this formula's quantities.")
    value = number(row["value"])
    if kind == "LHV" and value <= 0:
        raise ValueError("Heating values must be positive.")
    return value


def factor_term(factors, fid, quantity, unit, *, purpose="AVOIDED", snapshots=None):
    row = factors.get(str(fid))
    if not row or row["status"] not in {"APPROVED", "ACTIVE"} or row["config"]["purpose"] != purpose:
        raise Unquantified("A compatible approved factor is required.")
    conf = row["config"]
    if conf["numerator_unit"] not in {"kgCO2e", "tCO2e", "kgCO2"}:
        raise ValueError("Gas factors cannot be used as combined mitigation factors.")
    amount = convert(quantity, unit, conf["denominator_unit"]) * number(conf["value"])
    if conf["numerator_unit"] != "tCO2e":
        amount /= 1000
    if snapshots is not None:
        snapshots.append(factor_snapshot(row))
    return amount


def components(items, factors, snapshots):
    result = Decimal(0)
    for item in items:
        result += factor_term(factors, item["factor_id"], number(item["quantity"]), item["unit"], snapshots=snapshots)
    return result


def project_terms(ids, actual):
    if not ids:
        raise Unquantified("An empty component list does not establish zero project emissions.")
    if len(set(map(str, ids))) != len(ids):
        raise ValueError("Project components cannot be duplicated.")
    amount = Decimal(0)
    roots = set()
    for pid in ids:
        line = actual.get(str(pid))
        if not line or line["contribution_amount"] is None:
            raise Unquantified("Actual project components must be quantified.")
        root = line.get("linked_parent_id") or line["input_id"]
        if root in roots:
            raise ValueError("Parent and allocation/submeter components overlap.")
        roots.add(root)
        amount += Decimal(str(line["contribution_amount"]))
    return amount


def calculate_action(line, factors, parameters, actual, raw_inputs):
    base, project, action = line["baseline"], line["project"], line["action"]
    snapshots, terms = [], []
    baseline, project_amount = Decimal(0), None
    if action == "SOLAR":
        generated, used, exported, losses = (number(project[k]) for k in ("generated_kWh", "self_used_kWh", "exported_kWh", "losses_kWh"))
        displaced, export_eligible = number(base["displaced_self_kWh"]), number(base["eligible_export_kWh"])
        if generated != used + exported + losses or displaced > used or export_eligible > exported:
            raise ValueError("Solar production/use/export/loss readings do not reconcile.")
        baseline = factor_term(factors, base["displaced_grid_factor_id"], displaced, "kWh", snapshots=snapshots)
        project_amount = project_terms(project["project_component_input_ids"], actual)
        if exported > 0 and project_amount > 0:
            raise Unquantified("Self-use/export project components require a reviewed allocation before quantification.")
        terms.append({"name": "export_separate", "quantity": str(export_eligible), "unit": "kWh", "included_in_campus_total": False})
    elif action == "RAINWATER":
        volume = number(project["utilized_kL"])
        if base["route"] == "ENERGY":
            baseline_kwh = volume * parameter(parameters, base["energy_intensity_source_id"], "ENERGY_INTENSITY", {"kWh/kL", "kWh/m3"})
            baseline = factor_term(factors, base["energy_factor_id"], baseline_kwh, "kWh", snapshots=snapshots)
        elif base["route"] == "PROCESS_FACTOR":
            baseline = factor_term(factors, base["process_factor_id"], volume, "kL", snapshots=snapshots)
        else:
            raise ValueError("Unsupported rainwater baseline route.")
        baseline += components(base["other_components"], factors, snapshots)
        project_amount = project_terms(project["pumping_treatment_input_ids"] + project["other_component_input_ids"], actual)
    elif action == "BIOGAS":
        used, produced = number(project["used_std_m3"]), number(project["produced_std_m3"])
        if used > produced or (project["methane_fraction"] is not None and number(project["methane_fraction"]) > 1):
            raise ValueError("Biogas utilized volume/composition is invalid.")
        energy = used * parameter(parameters, project["LHV_source_id"], "LHV", {"MJ/std_m3"})
        efficiency = parameter(parameters, project["efficiency_source_id"], "EFFICIENCY", {"1", "fraction", "dimensionless"})
        if not 0 < efficiency <= 1:
            raise ValueError("Useful-output efficiency must be in (0,1].")
        useful = energy * efficiency
        supplied = project.get("useful_output")
        if project["route"] == "HEAT":
            if supplied:
                if supplied["unit"] != "MJ" or number(supplied["value"]) > useful:
                    raise ValueError("Metered heat exceeds supported utilized gas energy.")
                useful = number(supplied["value"])
            row = factors.get(str(base["energy_factor_id"]))
            if not row:
                raise Unquantified("Displaced fuel factor is unavailable.")
            heating_value = parameter(parameters, base["heating_value_source_id"], "LHV", {"MJ/" + row["config"]["denominator_unit"]})
            baseline_efficiency = parameter(parameters, base["efficiency_source_id"], "EFFICIENCY", {"1", "fraction", "dimensionless"})
            if heating_value <= 0 or not 0 < baseline_efficiency <= 1:
                raise ValueError("Baseline heat efficiency/heating value must be physically valid.")
            denominator = heating_value * baseline_efficiency
            baseline = factor_term(factors, base["energy_factor_id"], useful / denominator, row["config"]["denominator_unit"], snapshots=snapshots)
        elif project["route"] == "ELECTRICITY":
            if not supplied or supplied["unit"] != "kWh":
                raise Unquantified("Eligible displaced electricity requires supported useful output.")
            output = number(supplied["value"])
            if output > useful / Decimal("3.6"):
                raise ValueError("Displaced electricity exceeds available useful gas energy.")
            baseline = factor_term(factors, base["energy_factor_id"], output, "kWh", snapshots=snapshots)
        else:
            raise ValueError("Unsupported biogas output route.")
        baseline += components(base["treatment_components"], factors, snapshots)
        project_amount = project_terms(project["project_component_input_ids"], actual)
    elif action == "COMPOSTING":
        inputs = {str(row["id"]): row for row in raw_inputs}
        feedstock = [inputs.get(str(pid)) for pid in project["feedstock_input_ids"]]
        if not feedstock or any(row is None or row["category"] != "WASTE" or row["details"]["pathway"] != "COMPOSTING" or row["details"]["mass_basis"] != project["mass_basis"] for row in feedstock):
            raise Unquantified("Actual same-basis compost feedstock is required.")
        mass = sum((convert(number(row["quantity"]), row["unit"], "t") for row in feedstock), Decimal(0))
        baseline = factor_term(factors, base["disposal_factor_id"], mass, "t", snapshots=snapshots) + components(base["other_components"], factors, snapshots)
        project_amount = project_terms(project["actual_treatment_input_ids"] + project["other_component_input_ids"], actual)
    else:
        raise ValueError("Unsupported mitigation action.")
    return baseline - project_amount, baseline, project_amount, snapshots, terms


def calculate_mitigations(frozen, actual):
    avoided, output, unquantified, used_refs, overlaps = {}, [], [], set(), False
    for line in frozen.get("mitigations", []):
        action, amount, flags = line["action"], None, []
        refs = set()
        for key, value in line["project"].items():
            if key.endswith("_input_ids") or key == "meter_refs":
                refs.update(map(str, value))
        if refs & used_refs or line.get("overlap_refs"):
            overlaps = True
            flags.append("OVERLAP_REVIEW_REQUIRED")
        used_refs.update(refs)
        baseline, project, snapshots, terms = None, None, [], []
        try:
            amount, baseline, project, snapshots, terms = calculate_action(line, frozen["factors"], frozen["parameters"], actual, frozen["emissions"])
        except (Unquantified, ValueError, KeyError) as exc:
            flags.append("UNQUANTIFIED")
            unquantified.append(action)
        if action not in avoided:
            avoided[action] = amount
        elif avoided[action] is None or amount is None:
            avoided[action] = None
        else:
            avoided[action] += amount
        output.append({"id": str(uuid4()), "input_id": str(line["id"]), "formula_key": FORMULAS[action], "method_version": str(line["method_version"]), "normalized_quantity": None, "normalized_unit": None, "factor_snapshots": snapshots, "baseline_terms": [{"name": "matched_baseline", "result": baseline, "unit": "tCO2e"}] + terms, "project_terms": [{"name": "actual_project_components", "result": project, "unit": "tCO2e"}], "contribution_amount": amount, "contribution_unit": "tCO2e", "scope": None, "included_contribution": False, "linked_parent_id": None, "assumptions": [line["attributes"]["assessment_boundary"]], "flags": flags, "evidence_ids": list(map(str, line["evidence_ids"]))})
    total = sum((value for value in avoided.values() if value is not None), Decimal(0)) if avoided and not overlaps else None
    return avoided, total, sorted(set(unquantified)), output
