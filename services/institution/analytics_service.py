"""Bounded comparisons over frozen periods and their recorded denominators."""
from datetime import date
from decimal import Decimal, localcontext

from app.institution_errors import fail
from models.institution.common import normalize
from services.institution.audit_projection import denominators as project_denominators


def metric_value(result, metric):
    key = "quantified_avoided_total" if metric == "avoided_total" else metric
    value = result.get(key)
    return Decimal(value) if value is not None else None


def comparison_context(frozen, metric):
    audit = frozen["audit"]
    context = {"campus_ids": sorted(map(str, audit["campus_ids"])), "operational_control": sorted((str(row["id"]), row.get("control_input_hash")) for row in audit.get("boundary_snapshot", {}).get("campuses", []))}
    if metric in {"tree_stock", "removal", "release", "inventory_balance"}:
        context["tree_support"] = sorted((str(link["campus_id"]), str(link.get("attribution_snapshot", {}).get("boundary", {}).get("id") or link["assessment"].get("boundary_id")), str(link.get("attribution_snapshot", {}).get("mask", {}).get("id") or link["assessment"].get("mask_id"))) for link in frozen["tree_links"])
    return context


def emission_slice(output, frozen, campuses, scope=None, category=None):
    """Sum exact frozen contributing sources; never allocate missing campus IDs."""
    sources = {str(row["id"]): row for row in frozen["emissions"]}
    selected = set(campuses) or set(map(str, frozen["audit"]["campus_ids"]))
    if not selected <= set(map(str, frozen["audit"]["campus_ids"])):
        return None
    subset = selected != set(map(str, frozen["audit"]["campus_ids"]))
    relevant_categories = {category} if category else set(output["by_category"])
    if any(output["by_category"].get(key) is None for key in relevant_categories):
        return None
    if scope and scope not in output["by_scope"]:
        return None
    with localcontext() as context:
        context.prec = 50
        total = Decimal(0)
        for line in output["calculation_lines"]:
            source = sources.get(str(line["input_id"]))
            if source is None or source["category"] not in relevant_categories or not line["included_contribution"]:
                continue
            if scope and line["scope"] != scope:
                if line["scope"] is None:
                    return None
                continue
            if subset and source["campus_id"] is None:
                return None
            if source["campus_id"] is not None and str(source["campus_id"]) not in selected:
                continue
            if line["contribution_amount"] is None:
                return None
            total += Decimal(line["contribution_amount"])
        return total


def compare(previous, current, metric):
    a, b = metric_value(previous, metric), metric_value(current, metric)
    if a is None or b is None:
        return None, None, "Selected quantities are unavailable."
    flags = []
    if previous.get("_comparison_context") != current.get("_comparison_context"):
        flags.append("REPORTING_BOUNDARY_CHANGED")
    def scientific_coverage(value):
        if isinstance(value, dict):
            return {key: scientific_coverage(item) for key, item in value.items() if key not in {"evidence_ids", "reason"}}
        if isinstance(value, list):
            return [scientific_coverage(item) for item in value]
        return value
    if scientific_coverage(previous["coverage"]) != scientific_coverage(current["coverage"]):
        flags.append("COVERAGE_CHANGED")
    if previous["versions"]["method_version"] != current["versions"]["method_version"]:
        flags.append("METHOD_CHANGED")
    if previous["versions"]["factor_versions"] != current["versions"]["factor_versions"]:
        flags.append("FACTORS_CHANGED")
    if metric in {"tree_stock", "removal", "release", "inventory_balance"}:
        for key in ("model_versions", "quality_policy_versions", "feature_schema_versions"):
            if previous["versions"].get(key) != current["versions"].get(key):
                flags.append(key.upper() + "_CHANGED")
    span_a = (date.fromisoformat(previous["period_end"]) - date.fromisoformat(previous["period_start"])).days
    span_b = (date.fromisoformat(current["period_end"]) - date.fromisoformat(current["period_start"])).days
    if abs(span_a - span_b) > 1:
        flags.append("PERIOD_LENGTH_CHANGED")
    if flags:
        return None, None, ", ".join(flags)
    with localcontext() as context:
        context.prec = 50
        delta = b - a
        percentage = 100 * delta / a if a > 0 and b >= 0 and metric != "inventory_balance" else None
    return delta, percentage, None if percentage is not None else "Percentage is unavailable for zero, unknown or signed prior balance."


async def analytics(ctx):
    params = ctx.request.query_params
    metric = params.get("metric", "actual_total")
    if metric not in {"actual_total", "tree_stock", "removal", "release", "inventory_balance", "avoided_total"}:
        fail(422, "INVALID_METRIC", "Select a supported accounting metric.")
    if params.get("scope") and params["scope"] not in {"SCOPE_1", "SCOPE_2", "SCOPE_3"} or params.get("category") and params["category"] not in {"ELECTRICITY", "TRANSPORT", "STATIONARY_FUEL", "WASTE", "WATER"} or params.get("denominator") and params["denominator"] not in {"STUDENTS", "STAFF", "FLOOR_AREA"}:
        fail(422, "INVALID_FILTER", "Select a supported scope, category and denominator.")
    if params.get("period_start") and params.get("period_end") and params["period_start"] > params["period_end"]:
        fail(422, "INVALID_PERIOD", "The selected end date precedes its start.")
    ids = params.getlist("audit_ids")
    if len(ids) > 24:
        fail(422, "TOO_MANY_PERIODS", "Select at most 24 audit revisions.")
    rows = [await ctx.repo.get("institution_audits", aid) for aid in ids] if ids else await ctx.repo.find("institution_audits", {"institution_id": ctx.iid}, limit=1000)
    for row in rows:
        if str(row["institution_id"]) != ctx.iid or (ctx.member["permission"] == "VIEWER" and row["business_status"] not in {"FINALIZED", "SUPERSEDED"}):
            fail(404, "RESOURCE_NOT_FOUND", "Selected period is unavailable.")
    if not ids:
        finals = sorted((row for row in rows if row["business_status"] == "FINALIZED"), key=lambda r: r["period_end"], reverse=True)[:5]
        drafts = sorted((row for row in rows if row["business_status"] == "DRAFT"), key=lambda r: r["period_end"], reverse=True)
        rows = finals + (drafts[:1] if ctx.member["permission"] != "VIEWER" else [])
    if params.get("period_start"):
        rows = [r for r in rows if str(r["period_start"]) >= params["period_start"]]
    if params.get("period_end"):
        rows = [r for r in rows if str(r["period_end"]) <= params["period_end"]]
    rows.sort(key=lambda r: str(r["period_start"]))
    if not ids:
        while rows and (rows[-1]["period_end"] - rows[0]["period_start"]).days > 366 * 5:
            rows.pop(0)
    if rows and (rows[-1]["period_end"] - rows[0]["period_start"]).days > 366 * 5:
        fail(422, "ANALYTICS_SPAN_TOO_LARGE", "Select a reporting span of at most five years.")
    selected_campuses = set(params.getlist("campus_ids"))
    if len(selected_campuses) > 200:
        fail(422, "TOO_MANY_CAMPUSES", "Select at most 200 campuses.")
    for cid in selected_campuses:
        campus = await ctx.repo.get("campuses", cid)
        if str(campus["institution_id"]) != ctx.iid:
            fail(404, "RESOURCE_NOT_FOUND", "Selected campus is unavailable.")
    from services.institution.annual_service import result as load_result
    periods, results = [], []
    for audit in rows:
        calculated = bool(audit["reviewed_snapshot_id"] or audit["preview_snapshot_id"])
        if calculated:
            output = await load_result(ctx, audit)
            stored = await ctx.repo.get("institution_calculation_snapshots", output["snapshot_id"])
            frozen = stored["frozen_input"]
        else:
            output = {"snapshot_id": None, **{key: str(audit[key]) for key in ("period_start", "period_end", "timezone")}, "coverage": audit["coverage"], "actual_total": None, "by_scope": {}, "by_category": {}, "tree_stock": None, "removal": None, "release": None, "avoided_by_action": {}, "quantified_avoided_total": None, "inventory_balance": None}
        result = dict(output)
        if calculated:
            result["_comparison_context"] = comparison_context(frozen, metric)
        flags = [] if calculated else ["UNCALCULATED_DRAFT"]
        campus_subset = bool(selected_campuses and selected_campuses != set(map(str, audit["campus_ids"])))
        if campus_subset:
            # A whole-institution result cannot be presented as a campus subtotal.
            # Source allocation must be explicitly represented, never inferred.
            result.update(actual_total=None, tree_stock=None, removal=None, release=None, inventory_balance=None, quantified_avoided_total=None, by_scope={}, by_category={}, avoided_by_action={})
            flags.append("CAMPUS_SUBSET_NOT_A_SEPARATE_REVIEWED_INVENTORY")
        if params.get("scope") and not campus_subset:
            result["actual_total"] = output["by_scope"].get(params["scope"])
        if params.get("category") and not campus_subset:
            result["actual_total"] = output["by_category"].get(params["category"])
        if params.get("scope") and params.get("category"):
            # Separate marginal totals cannot identify their intersection.
            result["actual_total"] = None
            flags.append("SCOPE_CATEGORY_INTERSECTION_NOT_SEPARATELY_QUANTIFIED")
        if params.get("scope") or params.get("category"):
            result["inventory_balance"] = None
            flags.append("FILTERED_EMISSIONS_HAVE_NO_SEPARATE_TREE_ATTRIBUTION")
        if calculated and (campus_subset or params.get("scope") and params.get("category")):
            sliced = emission_slice(output, frozen, selected_campuses, params.get("scope"), params.get("category"))
            result["actual_total"] = sliced
            if campus_subset:
                links = [link for link in frozen["tree_links"] if str(link["campus_id"]) in selected_campuses]
                if {str(link["campus_id"]) for link in links} == selected_campuses:
                    with localcontext() as context:
                        context.prec = 50
                        for field, column in (("tree_stock", "stock_co2_t"), ("removal", "attributed_removal_tCO2"), ("release", "attributed_release_tCO2")):
                            values = [link["assessment"].get(column) if field == "tree_stock" else link.get(column) if link["comparability_status"] == "COMPARABLE" else None for link in links]
                            result[field] = sum((Decimal(value) for value in values), Decimal(0)) if all(value is not None for value in values) else None
                        if not params.get("scope") and not params.get("category") and all(result[key] is not None for key in ("actual_total", "removal", "release")):
                            result["inventory_balance"] = Decimal(result["removal"]) - Decimal(result["actual_total"]) - Decimal(result["release"])
            if sliced is not None:
                flags = [flag for flag in flags if flag not in {"CAMPUS_SUBSET_NOT_A_SEPARATE_REVIEWED_INVENTORY", "SCOPE_CATEGORY_INTERSECTION_NOT_SEPARATELY_QUANTIFIED"}]
                flags.append("EXPLICIT_FROZEN_SOURCE_SUBTOTAL")
        denominators = project_denominators(audit["denominators"])
        value = metric_value(result, "actual_total")
        intensities = {}
        for key, field in (("students", "students"), ("staff", "staff"), ("floor_area", "floor_area_m2")):
            denominator = denominators.get(field)
            with localcontext() as context:
                context.prec = 50
                intensities[key] = value / Decimal(str(denominator)) if value is not None and denominator is not None and Decimal(str(denominator)) > 0 and not campus_subset else None
        periods.append({"audit_id": str(audit["id"]), "snapshot_id": result["snapshot_id"], "revision": audit["revision"], **{key: result[key] for key in ("period_start", "period_end", "timezone", "coverage", "actual_total", "by_scope", "by_category", "tree_stock", "removal", "release", "avoided_by_action", "inventory_balance")}, "intensities": intensities, "comparability_flags": flags})
        results.append(result)
    delta, percentage, reason = compare(results[-2], results[-1], metric) if len(results) >= 2 else (None, None, "Two comparable calculated periods are required.")
    return normalize({"metric": metric, "periods": periods, "trend_delta": delta, "percent_change": percentage, "comparison_reason": reason, "warnings": []})
