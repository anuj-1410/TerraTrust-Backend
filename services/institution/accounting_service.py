"""Deterministic operational inventory. No model/provider/database side effects."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, localcontext
from uuid import uuid4

from app.institution_errors import fail
from models.institution.common import digest, normalize, numeric
from services.institution.factor_service import convert, compatible

CATEGORIES = ("ELECTRICITY", "TRANSPORT", "STATIONARY_FUEL", "WASTE", "WATER")
FORMULA = {"ELECTRICITY": "EM-GRID-01", "TRANSPORT": "EM-TRANSPORT-01", "STATIONARY_FUEL": "EM-STATIONARY-01", "WASTE": "EM-WASTE-01", "WATER": "EM-WATER-01"}


def issue(path, code, message):
    return {"path": path, "code": code, "message": message}


def validate_lines(audit, lines):
    errors = []
    ids = {str(row["id"]): row for row in lines}
    if len(ids) != len(lines):
        return [issue("/items", "DUPLICATE_ID", "Input IDs must be unique.")]
    streams, gases = defaultdict(list), set()
    for line in lines:
        path = "/emission_inputs/" + str(line["id"])
        if line["campus_id"] and str(line["campus_id"]) not in list(map(str, audit["campus_ids"])):
            errors.append(issue(path + "/campus_id", "CAMPUS_NOT_INCLUDED", "Campus is outside the reporting boundary."))
        if str(line["activity_start"]) < str(audit["period_start"]) or str(line["activity_end"]) > str(audit["period_end"]) or str(line["activity_end"]) < str(line["activity_start"]):
            errors.append(issue(path, "INVALID_ACTIVITY_PERIOD", "Activity must fall within the reporting period."))
        if line["quantity"] is not None and Decimal(str(line["quantity"])) < 0:
            errors.append(issue(path + "/quantity", "NEGATIVE_ACTIVITY", "Consumption cannot be negative."))
        if line["source_type"] == "ESTIMATED" and not line.get("estimate_notes"):
            errors.append(issue(path + "/estimate_notes", "ESTIMATE_NOTES_REQUIRED", "Explain the source and assumptions of this estimate."))
        parent_id = line.get("included_in_input_id")
        if parent_id:
            parent = ids.get(str(parent_id))
            if not parent or parent is line:
                errors.append(issue(path, "INVALID_INCLUDED_PARENT", "Select another source in this audit."))
            else:
                visited, node = {str(line["id"])}, parent
                while node:
                    nid = str(node["id"])
                    if nid in visited:
                        errors.append(issue(path, "INCLUSION_CYCLE", "Source inclusion cannot form a cycle."))
                        break
                    visited.add(nid)
                    node = ids.get(str(node.get("included_in_input_id")))
                if str(parent["activity_start"]) > str(line["activity_start"]) or str(parent["activity_end"]) < str(line["activity_end"]):
                    errors.append(issue(path, "INCOMPATIBLE_INCLUDED_PARENT", "Included activity must fit its parent's dates."))
                if line.get("allocation_ratio") is None and parent["unit"] != line["unit"]:
                    errors.append(issue(path, "UNIT_MISMATCH", "Submeter and main meter must use compatible activity units."))
        else:
            streams[(line["category"], str(line["campus_id"]), line["source_id"])].append(line)
        for gas in line["details"].get("gas_components") or []:
            gid = (gas["gas"], gas["stream_id"], str(line["activity_start"]), str(line["activity_end"]))
            if gid in gases or gas.get("included_combined_factor_id"):
                errors.append(issue(path, "DUPLICATE_GAS", "A gas stream cannot also contribute through another or combined factor."))
            gases.add(gid)
    for rows in streams.values():
        rows.sort(key=lambda r: str(r["activity_start"]))
        for a, b in zip(rows, rows[1:]):
            if str(b["activity_start"]) <= str(a["activity_end"]):
                errors.append(issue("/emission_inputs/" + str(b["id"]), "OVERLAPPING_ACTIVITY", "Annual/monthly source activity overlaps an already counted source."))
    allocations = defaultdict(list)
    for row in lines:
        if row.get("allocation_ratio") is not None:
            if not row.get("included_in_input_id"):
                errors.append(issue("/emission_inputs/" + str(row["id"]), "ALLOCATION_PARENT_REQUIRED", "Allocation requires a counted parent source."))
            allocations[str(row.get("included_in_input_id"))].append(Decimal(str(row["allocation_ratio"])))
    for parent_id, ratios in allocations.items():
        if any(r < 0 or r > 1 for r in ratios) or sum(ratios) > 1:
            errors.append(issue("/emission_inputs/" + parent_id, "INVALID_ALLOCATION", "Allocation shares must be nonnegative and cannot exceed one."))
    return errors


def factor_snapshot(row):
    config = row["config"]
    if not row.get("source_byte_hash"):
        raise ValueError("A factor snapshot requires the actual reviewed source byte checksum.")
    return {"id": str(row["id"]), "logical_key": row["logical_key"], "version": row["version"], "value": config["value"], "numerator_unit": config["numerator_unit"], "denominator_unit": config["denominator_unit"], "purpose": config["purpose"], "source_metadata": {"title": row["title"], "publisher": row["publisher"], "source_version": row["source_version"], "source_url": row.get("source_url"), "page": row.get("source_page"), "section": row.get("source_section"), "published_date": row.get("published_date"), "retrieved_at": row.get("validated_at"), "evidence_id": row.get("source_evidence_id"), "checksum": row["source_byte_hash"], "license": row["license"], "applicability_notes": row["applicability_notes"]}, "gas_coverage": {"basis": config["gas_basis"], "gwp": config.get("gwp"), "exclusions": config["exclusions"]}}


def gas_emissions(gases, factors, parameters):
    total, snapshots, terms = Decimal(0), [], []
    for gas in gases:
        if gas.get("included_combined_factor_id"):
            raise ValueError("Gas already included in a combined factor.")
        if gas["route"] == "MEASURED_MASS":
            if gas["mass_kg"] is None:
                return None, [], []
            mass = Decimal(str(gas["mass_kg"]))
        elif gas["route"] == "ACTIVITY_FACTOR":
            factor = factors.get(str(gas.get("gas_factor_id")))
            if not factor or gas["activity_quantity"] is None:
                return None, [], []
            conf = factor["config"]
            target = {"CH4": "kgCH4", "N2O": "kgN2O", "CO2_FOSSIL": "kgCO2", "CO2_BIOGENIC": "kgCO2"}[gas["gas"]]
            if conf["gas_basis"] != "GAS_SPECIFIC" or conf["numerator_unit"] != target or conf["purpose"] != "INVENTORY":
                raise ValueError("Gas factor has incompatible coverage.")
            mass = convert(Decimal(str(gas["activity_quantity"])), gas["activity_unit"], conf["denominator_unit"]) * Decimal(conf["value"])
            snapshots.append(factor_snapshot(factor))
        else:
            raise ValueError("Unsupported gas route.")
        if mass < 0:
            raise ValueError("Gas mass cannot be negative.")
        multiplier = Decimal(1)
        if gas["gas"] in {"CH4", "N2O"}:
            parameter = parameters.get(str(gas.get("gwp_source_id")))
            if not parameter or parameter["kind"] != "GWP" or parameter["value"] is None:
                return None, [], []
            multiplier = Decimal(str(parameter["value"]))
            if multiplier <= 0:
                raise ValueError("The reviewed GWP must be positive.")
        value = mass * multiplier / 1000
        biogenic = gas["gas"] == "CO2_BIOGENIC"
        if not biogenic:
            total += value
        terms.append({"gas": gas["gas"], "mass_kg": numeric(mass), "gwp": numeric(multiplier), "result": numeric(value), "biogenic_separate_disclosure": biogenic})
    return total, snapshots, terms


def calculate_line(audit, line, factors, parameters):
    conf = factors.get(str(line.get("factor_id")))
    result = {"id": str(uuid4()), "input_id": str(line["id"]), "formula_key": FORMULA[line["category"]], "method_version": str(audit["method_version"]), "normalized_quantity": None, "normalized_unit": line["unit"], "factor_snapshots": [], "baseline_terms": None, "project_terms": None, "contribution_amount": None, "contribution_unit": "tCO2e", "scope": None, "included_contribution": not bool(line.get("included_in_input_id")), "linked_parent_id": str(line["included_in_input_id"]) if line.get("included_in_input_id") else None, "assumptions": [], "flags": [], "evidence_ids": list(map(str, line["evidence_ids"]))}
    quantity = Decimal(str(line["quantity"])) if line["quantity"] is not None else None
    details = line["details"]
    if line["category"] == "STATIONARY_FUEL":
        count, net = details.get("consumed_cylinder_equivalents"), details.get("verified_net_kg_per_cylinder")
        if (count is None) != (net is None):
            raise ValueError("Both LPG cylinder fields are required.")
        if count is not None:
            derived = Decimal(count) * Decimal(net)
            if line["unit"] != "kg" or (quantity is not None and quantity != derived):
                raise ValueError("LPG cylinder mass conflicts with the supplied quantity.")
            quantity = derived
    if details.get("stock_reconciliation"):
        stock = {k: Decimal(v) for k, v in details["stock_reconciliation"].items() if k != "evidence_ids"}
        consumed = stock["opening_quantity"] + stock["purchased_quantity"] + stock["other_in_quantity"] - stock["closing_quantity"] - stock["other_out_quantity"]
        if consumed < 0 or quantity != consumed:
            raise ValueError("Fuel consumption conflicts with stock reconciliation.")
    gases = details.get("gas_components")
    if gases:
        if conf and conf["config"]["gas_basis"] == "COMBINED_CO2E":
            raise ValueError("Combined and gas-specific factors overlap.")
        amount, snapshots, terms = gas_emissions(gases, factors, parameters)
        result.update(formula_key="EM-GAS-01", contribution_amount=amount, factor_snapshots=snapshots, project_terms=terms, scope="SCOPE_1" if details.get("treatment_control", "ONSITE_CONTROLLED") == "ONSITE_CONTROLLED" else "SCOPE_3")
        return result
    if quantity is None or not conf:
        result["flags"].append("MISSING_ACTIVITY" if quantity is None else "MISSING_FACTOR")
        return result
    if not compatible(conf, line):
        raise ValueError("Pinned factor is incompatible with this activity.")
    factor = conf["config"]
    if Decimal(factor["value"]) < 0:
        raise ValueError("Emission factor values cannot be negative.")
    normalized = convert(quantity, line["unit"], factor["denominator_unit"], category=line["category"])
    if factor["numerator_unit"] not in {"kgCO2e", "tCO2e", "kgCO2"}:
        raise ValueError("Gas-specific factors require a gas component method.")
    amount = normalized * Decimal(factor["value"])
    if factor["numerator_unit"] != "tCO2e":
        amount /= 1000
    if factor["numerator_unit"] == "kgCO2":
        result["flags"].append("CO2_ONLY_FACTOR")
    if line["category"] == "WATER" and details["process"] == "PUMPING":
        result["formula_key"] = "EM-PUMP-01"
    result.update(normalized_quantity=normalized, normalized_unit=factor["denominator_unit"], factor_snapshots=[factor_snapshot(conf)], contribution_amount=amount, scope=factor["scope"])
    return result


def calculate_inventory(frozen, snapshot_id=None):
    """Reproduce one frozen snapshot; unknown terms remain unknown."""
    with localcontext() as ctx:
        ctx.prec = 50
        return _calculate(frozen, snapshot_id or str(uuid4()))


def _calculate(frozen, snapshot_id):
    audit, source = frozen["audit"], frozen["emissions"]
    factors, parameters = frozen["factors"], frozen["parameters"]
    errors = validate_lines(audit, source)
    lines = []
    for item in source:
        try:
            lines.append(calculate_line(audit, item, factors, parameters))
        except (ValueError, KeyError) as exc:
            errors.append(issue("/emission_inputs/" + str(item["id"]), "INVALID_ACTIVITY", str(exc)))
    by_id = {line["input_id"]: line for line in lines}
    originals = {str(item["id"]): item for item in source}
    def apply_allocation(line):
        if line["linked_parent_id"]:
            parent = by_id.get(line["linked_parent_id"])
            original = originals[line["input_id"]]
            if original.get("allocation_ratio") is not None:
                if not parent or parent["contribution_amount"] is None:
                    line["contribution_amount"] = None
                else:
                    line["contribution_amount"] = parent["contribution_amount"] * Decimal(str(original["allocation_ratio"]))
                    line["factor_snapshots"] = parent["factor_snapshots"]
                    line["scope"] = parent["scope"]
            line["flags"].append("ALREADY_INCLUDED_IN_PARENT")
    resolved = set()
    for line in lines:
        chain, seen, node = [], set(), line
        while node and node["input_id"] not in resolved and node["input_id"] not in seen:
            chain.append(node)
            seen.add(node["input_id"])
            node = by_id.get(node["linked_parent_id"])
        cycle = node is not None and node["input_id"] in seen
        for item in reversed(chain):
            if cycle:
                item["contribution_amount"] = None
            else:
                apply_allocation(item)
            resolved.add(item["input_id"])
    totals = {key: Decimal(0) for key in CATEGORIES}
    scopes = {key: Decimal(0) for key in audit["coverage"]["included_scopes"]}
    complete = not errors
    declarations = {row["category"]: row for row in audit["coverage"]["categories"]}
    for category in CATEGORIES:
        declaration = declarations.get(category)
        category_lines = [line for line in lines if next(x for x in source if str(x["id"]) == line["input_id"])["category"] == category]
        positive = any(line["contribution_amount"] is not None and line["contribution_amount"] > 0 for line in category_lines)
        state = declaration["state"] if declaration else "MISSING"
        if state == "MISSING" or (state == "REPORTED" and not category_lines) or (state in {"ZERO_CONFIRMED", "NOT_APPLICABLE"} and positive) or (state in {"ZERO_CONFIRMED", "NOT_APPLICABLE"} and (not declaration.get("reason") or not declaration.get("evidence_ids"))):
            errors.append(issue("/coverage/categories/" + category, "INCOMPLETE_DECLARATION", "Category coverage requires complete activity, evidence or a disclosed exclusion."))
            totals[category] = None
            complete = False
        for line in category_lines:
            if line["contribution_amount"] is None:
                if totals[category] is not None:
                    totals[category] = None
                complete = False
                errors.append(issue("/emission_inputs/" + line["input_id"], "MISSING_FACTOR_OR_ACTIVITY", "Complete the activity and approved factor."))
            elif line["included_contribution"]:
                if line["scope"] not in scopes:
                    complete = False
                    errors.append(issue("/emission_inputs/" + line["input_id"], "SCOPE_NOT_INCLUDED", "This actual source belongs to an excluded scope."))
                else:
                    scopes[line["scope"]] += line["contribution_amount"]
                if totals[category] is not None:
                    totals[category] += line["contribution_amount"]
    subtotal = sum((line["contribution_amount"] for line in lines if line["included_contribution"] and line["contribution_amount"] is not None), Decimal(0))
    actual = subtotal if complete else None
    tree_links = frozen.get("tree_links", [])
    missing = set(map(str, audit["campus_ids"])) - {str(link["campus_id"]) for link in tree_links}
    stock_complete = not missing and bool(tree_links)
    known_stock = sum((Decimal(str(link["assessment"]["stock_co2_t"])) for link in tree_links if link["assessment"].get("stock_co2_t") is not None), Decimal(0)) if tree_links else None
    stock_complete = stock_complete and all(link["assessment"].get("stock_co2_t") is not None for link in tree_links)
    comparable = stock_complete and all(link["comparability_status"] == "COMPARABLE" and link.get("attributed_removal_tCO2") is not None and link.get("attributed_release_tCO2") is not None for link in tree_links)
    removal = sum((Decimal(str(link["attributed_removal_tCO2"])) for link in tree_links), Decimal(0)) if comparable else None
    release = sum((Decimal(str(link["attributed_release_tCO2"])) for link in tree_links), Decimal(0)) if comparable else None
    balance = removal - actual - release if comparable and actual is not None else None
    from services.institution.mitigation_service import calculate_mitigations
    avoided, avoided_total, unquantified, mitigation_lines = calculate_mitigations(frozen, by_id)
    quality = []
    if not complete:
        quality.append("INCOMPLETE")
    if not comparable:
        quality.append("BASELINE_ONLY" if tree_links and all(link["comparability_status"] == "BASELINE_ONLY" for link in tree_links) else "NONCOMPARABLE")
    quality.append("UNKNOWN_UNCERTAINTY")
    method = frozen["method"]
    result = {"id": None, "snapshot_id": snapshot_id, "audit_id": str(audit["id"]), "logical_audit_id": str(audit["logical_audit_id"]), "revision": audit["revision"], "input_revision": audit["input_revision"], "period_start": str(audit["period_start"]), "period_end": str(audit["period_end"]), "timezone": audit["timezone"], "coverage": audit["coverage"], "exclusions": audit["coverage"]["exclusions"], "by_scope": scopes if complete else {key: None for key in scopes}, "by_category": totals, "actual_total": actual, "quantified_actual_subtotal": subtotal, "tree_stock": known_stock if stock_complete else None, "tree_stock_tC": sum((Decimal(str(link["assessment"]["stock_tC"])) for link in tree_links), Decimal(0)) if stock_complete else None, "removal": removal, "release": release, "tree_coverage": {"stock_complete": stock_complete, "change_complete": comparable, "missing_campus_ids": sorted(missing), "stock_subtotal_tCO2": known_stock}, "avoided_by_action": avoided, "quantified_avoided_total": avoided_total, "unquantified_actions": unquantified, "inventory_balance": balance, "scenario_balance": None, "balance_status": None, "quality_flags": quality, "uncertainty": {"type": "UNKNOWN", "lower": None, "upper": None, "confidence_level": None, "method_version": None, "dependence_assumptions": None, "sources": []}, "input_hash": frozen["input_hash"], "result_hash": "", "calculation_lines": lines + mitigation_lines, "versions": {"schema_version": 1, "method_version": str(method["id"]), "model_versions": sorted({str(link["assessment"]["model_version"]) for link in tree_links}), "factor_versions": sorted(factors), "quality_policy_versions": sorted({str(link["assessment"]["provenance"]["quality_policy_version"]) for link in tree_links if link["assessment"].get("provenance", {}).get("quality_policy_version")}), "feature_schema_versions": sorted({str(link["assessment"]["feature_schema_version"]) for link in tree_links}), "formula_keys": sorted({line["formula_key"] for line in lines + mitigation_lines}), "algorithm_versions": {"canonicalization": "SCJ1", "measurement": "MCJ1", "accounting": "TT_INSTITUTION_1"}}}
    normalized = normalize(result)
    hashed = {k: v for k, v in normalized.items() if k not in {"id", "snapshot_id", "result_hash"}}
    hashed["calculation_lines"] = [{k: v for k, v in line.items() if k != "id"} for line in normalized["calculation_lines"]]
    normalized["result_hash"] = digest(hashed)
    return normalized, errors
