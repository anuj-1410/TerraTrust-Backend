"""Explicit hypothetical project comparison; never edits measured inventory."""
from decimal import Decimal, localcontext
from uuid import uuid4

from app.institution_access import require_flow
from app.institution_errors import fail
from models.institution.common import digest, normalize
from models.institution.contracts import parse
from services.institution import mitigation_service, accounting_service
from services.institution.base import evidence
from services.institution.factor_service import approved


async def create(ctx, body):
    require_flow(advisor=True)
    audit = ctx.parent
    snapshot = await ctx.repo.get("institution_calculation_snapshots", body["snapshot_id"])
    if str(snapshot["audit_id"]) != str(audit["id"]):
        fail(404, "RESOURCE_NOT_FOUND", "Scenario context is unavailable.")
    action = body["action"]
    if action not in mitigation_service.FORMULAS or body["formula_key"] != mitigation_service.FORMULAS[action]:
        fail(422, "UNSUPPORTED_SCENARIO", "Select an implemented approved project formula.")
    frozen = snapshot["frozen_input"]
    method = frozen["method"]
    if body["formula_key"] not in method["config"]["formula_keys"]:
        fail(422, "UNSUPPORTED_SCENARIO", "The selected method has not approved this scenario formula.")
    if set(body["assumptions"]) != {"baseline", "project", "attributes"}:
        fail(422, "INVALID_SCENARIO_ASSUMPTIONS", "Supply the exact action baseline, project and attributes.")
    line = {"id": str(uuid4()), "action": action, "campus_id": None, "activity_start": str(audit["period_start"]), "activity_end": str(audit["period_end"]), "method_version": str(method["id"]), "evidence_ids": body["evidence_ids"], "overlap_refs": [], **body["assumptions"]}
    try:
        parse("MitigationLine", line)
    except ValueError:
        fail(422, "INVALID_SCENARIO_ASSUMPTIONS", "Complete the typed baseline and project assumptions.")
    await evidence(ctx.repo, body["evidence_ids"], "AUDIT", audit["id"], ctx.iid)
    factors = dict(frozen["factors"])
    allowed = {fid for mapping in method["config"]["factor_mappings"] for fid in mapping["allowed_factor_ids"]}
    async def collect(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key.endswith("factor_id") and value:
                    if value not in allowed:
                        fail(422, "INCOMPATIBLE_FACTOR", "Scenario factors must be explicitly allowed by this method.")
                    factor = await approved(ctx.repo, "emission_factors", value)
                    source = await ctx.repo.get("institution_audit_evidence",factor["source_evidence_id"])
                    if source["state"] != "READY" or source["bytes_deleted_at"]:
                        fail(422,"SOURCE_BYTES_REQUIRED","Scenario factor source bytes are unavailable.")
                    factor["source_byte_hash"] = source["byte_hash"]
                    if factor["valid_from"] and str(factor["valid_from"]) > str(audit["period_start"]) or factor["valid_to"] and str(factor["valid_to"]) < str(audit["period_end"]):
                        fail(422, "INCOMPATIBLE_FACTOR", "Scenario factor does not cover the full assessed period.")
                    factors[value] = normalize(factor)
                else:
                    await collect(value)
        elif isinstance(node, list):
            for value in node:
                await collect(value)
    await collect(line)
    actual = {r["input_id"]: r for r in snapshot["result"]["calculation_lines"] if not r["formula_key"].startswith("MIT-")}
    errors, amount, factor_snapshots = [], None, []
    with localcontext() as precision:
        precision.prec = 50
        try:
            amount, baseline, project, factor_snapshots, terms = mitigation_service.calculate_action(line, factors, frozen["parameters"], actual, frozen["emissions"])
        except (ValueError, KeyError, mitigation_service.Unquantified):
            errors = [{"path":"/assumptions", "code":"SCENARIO_INPUTS_REQUIRED", "message":"Quantification requires complete explicit source quantities, factors and project components."}]
    if errors:
        fail(422, "SCENARIO_INPUTS_REQUIRED", "Complete the hypothetical assumptions before calculation.", fields=errors)
    hashed = digest({"snapshot_id": body["snapshot_id"], "snapshot_hash": snapshot["result_hash"], "formula_key": body["formula_key"], "assumptions": body["assumptions"], "factors": factor_snapshots, "constraints": body["constraints"]})
    row = await ctx.repo.insert("advisory_scenarios", {"institution_id": ctx.iid, "audit_id": audit["id"], "snapshot_id": snapshot["id"], "created_by": ctx.actor["id"], "formula_key": body["formula_key"], "assumptions": body["assumptions"], "factor_snapshots": factor_snapshots, "result": {"value": normalize(amount), "units":"tCO2e"}, "overlap_flags": [], "method_version": method["id"], "input_hash": hashed, "hypothetical": True})
    await ctx.repo.event(ctx.actor, "HYPOTHETICAL_SCENARIO_SAVED", "advisory_scenarios", row)
    return {"id": str(row["id"]), "audit_id": str(audit["id"]), "snapshot_id": str(snapshot["id"]), "assumptions": body["assumptions"], "factors": factor_snapshots, "method_version": str(method["id"]), "result": normalize(amount), "units":"tCO2e", "missing_data": [], "overlap_flags": [], "input_hash": hashed, "hypothetical": True}
