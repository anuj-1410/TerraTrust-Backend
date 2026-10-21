"""Annual period, draft, freeze, correction and exact-source snapshot lifecycle."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from uuid import uuid4

from sqlalchemy import text

from app.institution_access import require_flow
from app.institution_errors import fail
from models.institution.common import digest, normalize
from models.institution.contracts import parse, MODELS
from services.institution.base import draft, evidence, link_evidence, public, case_for
from services.institution.factor_service import approved, resolve_factor, compile_schema
from services.institution.accounting_service import validate_lines, calculate_inventory, CATEGORIES
from services.institution.job_service import enqueue, accepted

INPUT_KEYS = {"institution_emission_inputs": set(MODELS["EmissionLine"].model_fields), "institution_mitigation_inputs": set(MODELS["MitigationLine"].model_fields)}


def line_public(table, row):
    return {key: normalize(row.get(key)) for key in INPUT_KEYS[table] if key in row}


async def content(ctx, audit):
    emissions = await ctx.repo.find("institution_emission_inputs", {"audit_id": audit["id"]})
    mitigations = await ctx.repo.find("institution_mitigation_inputs", {"audit_id": audit["id"]})
    links = await ctx.repo.find("institution_audit_tree_links", {"audit_id": audit["id"]})
    emissions.sort(key=lambda row: str(row["id"]))
    mitigations.sort(key=lambda row: str(row["id"]))
    links.sort(key=lambda row: str(row["campus_id"]))
    base = {key: normalize(audit[key]) for key in ("id", "logical_audit_id", "revision", "period_start", "period_end", "timezone", "label", "campus_ids", "coverage", "denominators", "boundary_snapshot", "method_version")}
    base["emissions"] = sorted([line_public("institution_emission_inputs", row) for row in emissions], key=lambda r: r["id"])
    base["mitigations"] = sorted([line_public("institution_mitigation_inputs", row) for row in mitigations], key=lambda r: r["id"])
    base["tree_links"] = sorted([normalize({key: row[key] for key in ("campus_id", "campus_tree_audit_id", "period_start", "period_end", "comparability_status", "attributed_removal_tCO2", "attributed_release_tCO2", "attribution_snapshot")}) for row in links], key=lambda r: r["campus_id"])
    return base


async def refresh_hash(ctx, audit):
    hashed = digest(await content(ctx, audit))
    saved = await ctx.repo.update("institution_audits", audit["id"], {"input_hash": hashed, "preview_snapshot_id": None}, bump=True)
    ctx.revision = saved["input_revision"]
    await ctx.repo.event(ctx.actor, "AUDIT_INPUT_SAVED", "institution_audits", saved, old_hash=audit["input_hash"])
    return saved


async def create(ctx, body):
    require_flow()
    method = await approved(ctx.repo, "carbon_methodologies", body["method_version"])
    if set(row["category"] for row in body["coverage"]["categories"]) != set(CATEGORIES) or len(body["coverage"]["categories"]) != len(CATEGORIES):
        fail(422, "INVALID_COVERAGE", "Declare each expected category exactly once.")
    if body["coverage"]["consolidation_approach"] != "OPERATIONAL_CONTROL":
        fail(422, "UNSUPPORTED_CONSOLIDATION", "The initial methodology uses operational control.")
    await ctx.repo.get("institutions", ctx.iid, lock=True)
    campuses = []
    if not body["campus_ids"] or len(set(body["campus_ids"])) != len(body["campus_ids"]):
        fail(422, "CAMPUS_REQUIRED", "Select unique reporting campuses.")
    for cid in body["campus_ids"]:
        campus = await ctx.repo.get("campuses", cid)
        if str(campus["institution_id"]) != ctx.iid or campus["control_status"] != "APPROVED" or campus["archived_at"]:
            fail(422, "CAMPUS_NOT_APPROVED", "Every selected campus must have approved control.")
        campuses.append(normalize(campus))
    for declaration in body["coverage"]["categories"]:
        # A new audit cannot have AUDIT-parent evidence before it exists. Empty
        # declarations remain incomplete until normal evidence uploads and saves.
        if declaration["evidence_ids"]:
            fail(422, "EVIDENCE_PARENT_REQUIRED", "Create the period before attaching audit-owned evidence.")
    period = await ctx.repo.insert("institution_audit_periods", {"institution_id": ctx.iid, "created_by": ctx.actor["id"], **{key: body[key] for key in ("period_start", "period_end", "timezone", "label")}})
    audit = await ctx.repo.insert("institution_audits", {"institution_id": ctx.iid, "logical_audit_id": period["id"], "revision": 1, "created_by": ctx.actor["id"], "business_status": "DRAFT", "processing_status": "IDLE", "input_hash": digest(body), "boundary_snapshot": {"institution": normalize(ctx.parent), "campuses": campuses}, **body})
    await ctx.repo.update("institution_audit_periods", period["id"], {"active_draft_audit_id": audit["id"]})
    audit = await ctx.repo.update("institution_audits", audit["id"], {"input_hash": digest(await content(ctx, audit))})
    await ctx.repo.event(ctx.actor, "AUDIT_CREATED", "institution_audits", audit)
    ctx.revision = 1
    return await resource(ctx, audit)


async def freeze(ctx, audit):
    method = await ctx.repo.get("carbon_methodologies", audit["method_version"])
    emissions = await ctx.repo.find("institution_emission_inputs", {"audit_id": audit["id"]})
    mitigations = await ctx.repo.find("institution_mitigation_inputs", {"audit_id": audit["id"]})
    links = await ctx.repo.find("institution_audit_tree_links", {"audit_id": audit["id"]})
    emissions.sort(key=lambda row: str(row["id"]))
    mitigations.sort(key=lambda row: str(row["id"]))
    links.sort(key=lambda row: str(row["campus_id"]))
    factors, parameters, evidence_manifest, project_methods = {}, {}, {}, {}
    method_ids = {str(audit["method_version"]), *map(lambda row: str(row["method_version"]), mitigations)}
    for mid in sorted(method_ids):
        selected = await ctx.repo.get("carbon_methodologies", mid)
        project_methods[mid] = normalize(selected)
        params = await ctx.repo.find("method_parameters", {"methodology_id": mid})
        parameters.update({str(p["id"]): normalize(p) for p in params})
    async def collect(node, key=""):
        if isinstance(node, dict):
            for key, value in node.items():
                if key.endswith("factor_id") and value:
                    row = await ctx.repo.get("emission_factors", str(value))
                    if row["source_evidence_id"]:
                        source = await ctx.repo.get("institution_audit_evidence", row["source_evidence_id"])
                        row["source_byte_hash"] = source["byte_hash"]
                    elif row["source_version"].startswith("DATABASE_CONFIGURATION:") and row["license"] == "PROJECT_CONFIGURATION":
                        row["source_byte_hash"] = row["artifact_hash"]
                    else:
                        fail(422, "SOURCE_BYTES_REQUIRED", "This factor has neither source bytes nor administrator-maintained configuration.")
                    factors[str(row["id"])] = normalize(row)
                elif key == "evidence_ids" and value:
                    manifest = await evidence(ctx.repo, list(map(str, value)), "AUDIT", audit["id"], ctx.iid)
                    evidence_manifest.update({item["id"]: item for item in manifest})
                else:
                    await collect(value, key)
        elif isinstance(node, list):
            for value in node:
                await collect(value, key)
    await collect(normalize(emissions + mitigations))
    await collect(audit["coverage"])
    await collect(audit["denominators"])
    tree_links = []
    for link in links:
        tree = await ctx.repo.get("campus_tree_audits", link["campus_tree_audit_id"])
        tree_links.append({**normalize(link), "assessment": normalize(tree)})
    frozen = {"audit": normalize(audit), "emissions": normalize(emissions), "mitigations": normalize(mitigations), "tree_links": tree_links, "factors": factors, "parameters": parameters, "method": normalize(method), "project_methods": project_methods, "evidence_manifest": evidence_manifest, "input_hash": audit["input_hash"]}
    return frozen


async def validation(ctx, audit):
    frozen = await freeze(ctx, audit)
    _, errors = calculate_inventory(frozen)
    today = datetime.now(ZoneInfo(audit["timezone"])).date()
    if date.fromisoformat(str(audit["period_end"])) >= today:
        errors.append({"path": "/period_end", "code": "PERIOD_NOT_ENDED", "message": "The complete reporting period must end before submission."})
    sources = {}
    for line in frozen["emissions"]:
        if line["included_in_input_id"]:
            continue
        sources.setdefault((line["category"], str(line.get("campus_id")), line["source_id"]), []).append(line)
    for source_id, rows in sources.items():
        rows.sort(key=lambda row: row["activity_start"])
        next_date = date.fromisoformat(str(audit["period_start"]))
        for row in rows:
            if date.fromisoformat(row["activity_start"]) != next_date:
                errors.append({"path": "/emission_inputs/" + row["id"], "code": "MISSING_SOURCE_COVERAGE", "message": "Explain missing source periods with the selected method; missing months are not zero."})
            next_date = date.fromisoformat(row["activity_end"]) + timedelta(days=1)
        if next_date != date.fromisoformat(str(audit["period_end"])) + timedelta(days=1):
            errors.append({"path": "/emission_inputs", "code": "MISSING_SOURCE_COVERAGE", "message": "Source activity does not cover the complete period."})
    return {"input_revision": audit["input_revision"], "errors": errors, "warnings": [], "can_submit": not errors, "coverage": audit["coverage"]}


async def resource(ctx, audit):
    cases = await ctx.repo.find("review_cases", {"resource_id": audit["id"], "resource_type": "INSTITUTION_AUDIT"})
    jobs = await ctx.repo.find("processing_jobs", {"resource_id": audit["id"], "job_type": "institution_calculation", "input_hash": audit["input_hash"]})
    if audit["business_status"] != "DRAFT" or ctx.member and ctx.member["permission"] == "VIEWER":
        check = {"input_revision": audit["input_revision"], "errors": [], "warnings": [], "can_submit": False, "coverage": audit["coverage"]}
    else:
        check = await validation(ctx, audit)
    return public("AuditResource", audit, validation=check, copied_id_mappings=[], review_case_id=cases[0]["id"] if cases else None, calculation_job_id=jobs[0]["id"] if jobs else None)


async def write_lines(ctx, body, *, mitigation=False, append=False, patch_id=None, delete_id=None):
    require_flow()
    audit = await ctx.repo.get("institution_audits", ctx.parent["id"], lock=True)
    ctx.match(audit)
    draft(audit, "business_status")
    table = "institution_mitigation_inputs" if mitigation else "institution_emission_inputs"
    discriminator = "action" if mitigation else "category"
    existing = await ctx.repo.find(table, {"audit_id": audit["id"]})
    by_id = {str(row["id"]): row for row in existing}
    if patch_id or delete_id:
        old = by_id.get(patch_id or delete_id)
        if old is None:
            fail(404, "RESOURCE_NOT_FOUND", "Input is unavailable.")
        selected = old[discriminator]
        if delete_id:
            dependents = [r for r in existing if str(r.get("included_in_input_id")) == delete_id]
            mitigations = await ctx.repo.find("institution_mitigation_inputs", {"audit_id": audit["id"]})
            if dependents or any(delete_id in str(r["project"]) or delete_id in str(r["overlap_refs"]) for r in mitigations if str(r["id"]) != delete_id):
                fail(409, "DEPENDENT_INPUTS_EXIST", "Resolve dependent allocation or project references first.")
            items = []
            resulting = [r for r in existing if str(r["id"]) != delete_id]
        else:
            if set(body) - (INPUT_KEYS[table] - {"id", discriminator}):
                fail(422, "INVALID_PAYLOAD", "Identity and category/action cannot change.")
            item = {**line_public(table, old), **body}
            parse("MitigationLine" if mitigation else "EmissionLine", item)
            items = [item]
            resulting = [r for r in existing if str(r["id"]) != patch_id] + items
    elif append:
        item = body["item"]
        selected, items = item[discriminator], [item]
        if item["id"] in by_id:
            fail(409, "DUPLICATE_INPUT", "Use the existing line's edit endpoint.")
        resulting = existing + items
    else:
        selected, items = body[discriminator], body["items"]
        if any(item[discriminator] != selected for item in items):
            fail(422, "CATEGORY_MISMATCH", "Every replacement item must belong to the selected category/action.")
        resulting = [r for r in existing if r[discriminator] != selected] + items
        deleted = set(by_id) - {str(row["id"]) for row in resulting}
        if set(body.get("deletion_ids", [])) != deleted:
            fail(409, "DELETION_INTENT_REQUIRED", "List every removed input explicitly before replacing this category.")
    for item in items:
        collision = await ctx.repo.get(table, item["id"], required=False)
        if collision and str(collision["audit_id"]) != str(audit["id"]):
            fail(409, "DUPLICATE_INPUT", "Input identity is already in use.")
        await evidence(ctx.repo, item["evidence_ids"], "AUDIT", audit["id"], ctx.iid)
        if date.fromisoformat(str(item["activity_end"])) > datetime.now(ZoneInfo(audit["timezone"])).date():
            fail(422, "FUTURE_ACTIVITY", "Future activity cannot be saved as measured activity.")
        method_id = item["method_version"] if mitigation else str(audit["method_version"])
        method = await approved(ctx.repo, "carbon_methodologies", method_id)
        schema = method["config"]["activity_schemas"].get(selected)
        if not schema:
            fail(503, "METHOD_NOT_CONFIGURED", "This category/action has no approved form schema.")
        errors = list(compile_schema(schema).iter_errors(item if mitigation else item["details"]))
        if errors:
            fail(422, "METHOD_INPUT_INVALID", "Input does not match the approved method schema.")
        if not mitigation:
            item["factor_id"] = await resolve_factor(ctx.repo, method, item)
        else:
            if item["campus_id"] and item["campus_id"] not in list(map(str, audit["campus_ids"])):
                fail(422, "CAMPUS_NOT_INCLUDED", "Mitigation campus is outside this audit.")
            for key, refs in item["project"].items():
                if key.endswith("_input_ids"):
                    for ref in refs:
                        source_row = await ctx.repo.get("institution_emission_inputs", ref)
                        if str(source_row["audit_id"]) != str(audit["id"]) or str(source_row["activity_start"]) < str(item["activity_start"]) or str(source_row["activity_end"]) > str(item["activity_end"]):
                            fail(422, "PROJECT_COMPONENT_MISMATCH", "Project components must be actual inputs in this audit and action period.")
    if not mitigation:
        errors = validate_lines(audit, resulting)
        if errors:
            fail(422, "INVALID_ACTIVITY", "Correct source overlap, periods or inclusion.", fields=errors)
        declaration = body.get("declaration")
        if declaration:
            if declaration["category"] != selected:
                fail(422, "CATEGORY_MISMATCH", "Declaration category must match the activity.")
            await validate_declaration(ctx, audit, declaration, resulting)
            coverage = {**audit["coverage"], "categories": [declaration if d["category"] == selected else d for d in audit["coverage"]["categories"]]}
            audit = await ctx.repo.update("institution_audits", audit["id"], {"coverage": coverage})
    removed = set(by_id) - {str(row["id"]) for row in resulting}
    if removed and not mitigation:
        mitigations = await ctx.repo.find("institution_mitigation_inputs", {"audit_id": audit["id"]})
        if any(any(rid in str(action["project"]) or rid in str(action["overlap_refs"]) for rid in removed) for action in mitigations):
            fail(409, "DEPENDENT_INPUTS_EXIST", "Resolve project component references before removing actual inputs.")
    for rid in removed:
        await ctx.repo.conn.execute(text(f"DELETE FROM {table} WHERE id=:id"), {"id": rid})
    for item in items:
        values = {key: value for key, value in item.items() if key in INPUT_KEYS[table]}
        if item["id"] in by_id:
            await ctx.repo.update(table, item["id"], values, bump=True)
        else:
            await ctx.repo.insert(table, {**values, "institution_id": ctx.iid, "audit_id": audit["id"], "created_by": ctx.actor["id"], "flags": []})
    audit = await refresh_hash(ctx, audit)
    if delete_id:
        return {"deleted_id": delete_id, "input_revision": ctx.revision}
    result = {"input_revision": ctx.revision}
    if append or patch_id:
        result["item"] = line_public(table, await ctx.repo.get(table, items[0]["id"]))
    else:
        result["items"] = [line_public(table, r) for r in await ctx.repo.find(table, {"audit_id": audit["id"], discriminator: selected})]
    if not mitigation and body.get("declaration"):
        result["declaration"] = body["declaration"]
    return result


async def validate_declaration(ctx, audit, declaration, lines):
    rows = [r for r in lines if r["category"] == declaration["category"]]
    positive = any(r.get("quantity") is not None and float(r["quantity"]) > 0 for r in rows)
    if declaration["state"] in {"ZERO_CONFIRMED", "NOT_APPLICABLE"}:
        if positive or not declaration["reason"]:
            fail(422, "INVALID_DECLARATION", "A zero/exclusion declaration cannot hide positive activity and needs a reason.")
        await evidence(ctx.repo, declaration["evidence_ids"], "AUDIT", audit["id"], ctx.iid, required=True)
    else:
        await evidence(ctx.repo, declaration["evidence_ids"], "AUDIT", audit["id"], ctx.iid)


async def calculate(ctx, body, *, submit=False):
    require_flow()
    audit = await ctx.repo.get("institution_audits", ctx.parent["id"], lock=True)
    ctx.match(audit)
    if audit["business_status"] not in ({"DRAFT"} if submit else {"DRAFT", "SUBMITTED", "UNDER_REVIEW"}):
        fail(409, "RESOURCE_FROZEN", "This revision cannot accept a new calculation.")
    if body["expected_input_hash"] != audit["input_hash"]:
        fail(409, "INPUT_HASH_CHANGED", "The saved input hash changed. Refresh before calculating.")
    if not submit and body["mode"] != "PREVIEW":
        fail(422, "INVALID_CALCULATION_MODE", "Only PREVIEW is accepted.")
    await approved(ctx.repo, "carbon_methodologies", audit["method_version"])
    frozen = await freeze(ctx, audit)
    if submit:
        check = await validation(ctx, audit)
        if check["errors"]:
            fail(422, "INCOMPLETE_AUDIT", "Resolve submission blockers before freezing this revision.", fields=check["errors"])
        await evidence(ctx.repo, body["evidence_ids"], "AUDIT", audit["id"], ctx.iid, required=True)
        selected_evidence = set(frozen["evidence_manifest"])
        if not selected_evidence <= set(body["evidence_ids"]):
            fail(422, "EVIDENCE_MISSING", "Submission must include every source file used by this revision.")
        audit = await ctx.repo.update("institution_audits", audit["id"], {"business_status": "SUBMITTED", "frozen_input_hash": audit["input_hash"], "frozen_snapshot": frozen, "submitted_by": ctx.actor["id"], "submitted_at": datetime.now(timezone.utc)})
    job = await enqueue(ctx, "institution_calculation", audit, frozen, asset_type="INSTITUTION_AUDIT")
    await ctx.repo.update("institution_audits", audit["id"], {"processing_status": job["status"]})
    ctx.revision = audit["input_revision"]
    if submit:
        case = await case_for(ctx.repo, ctx.actor, "institution_audits", audit, "INSTITUTION_AUDIT", {**await resource(ctx, audit), "frozen_input": frozen})
        return await resource(ctx, await ctx.repo.get("institution_audits", audit["id"]))
    return accepted(job)


async def result(ctx, audit, snapshot_id=None):
    if ctx.member and ctx.member["permission"] == "VIEWER" and audit["business_status"] not in {"FINALIZED", "SUPERSEDED"}:
        fail(404, "RESOURCE_NOT_FOUND", "Result is unavailable.")
    sid = snapshot_id or audit.get("reviewed_snapshot_id") or audit.get("preview_snapshot_id")
    if not sid:
        fail(409, "RESULT_NOT_READY", "No calculated snapshot is ready for this revision.")
    snapshot = await ctx.repo.get("institution_calculation_snapshots", sid)
    if str(snapshot["audit_id"]) != str(audit["id"]):
        fail(404, "RESOURCE_NOT_FOUND", "Snapshot is unavailable.")
    if ctx.member and ctx.member["permission"] == "VIEWER" and str(audit["reviewed_snapshot_id"]) != str(sid):
        fail(404, "RESOURCE_NOT_FOUND", "Snapshot is unavailable.")
    ctx.context_id = str(snapshot["id"])
    output = dict(snapshot["result"])
    if audit["current_result_id"] and str(audit["reviewed_snapshot_id"]) == str(sid):
        output["id"] = str(audit["current_result_id"])
    return output


async def revision(ctx, body):
    require_flow()
    old = ctx.parent
    family = await ctx.repo.get("institution_audit_periods", old["logical_audit_id"], lock=True)
    old = await ctx.repo.get("institution_audits", old["id"], lock=True)
    ctx.match(old)
    if body["source_input_revision"] != old["input_revision"] or old["business_status"] not in {"NEEDS_CORRECTION", "FINALIZED"} or family["active_draft_audit_id"]:
        fail(409, "REVIEW_IN_PROGRESS", "Only one working correction may exist in a reporting family.")
    if old["business_status"] == "FINALIZED":
        prior = await result(ctx, old)
        if body["source_result_hash"] != prior["result_hash"]:
            fail(409, "INPUT_HASH_CHANGED", "Correction must name the original final result hash.")
    all_revisions = await ctx.repo.find("institution_audits", {"logical_audit_id": old["logical_audit_id"]})
    keys = ("period_start", "period_end", "timezone", "label", "campus_ids", "coverage", "denominators", "boundary_snapshot", "method_version")
    new = await ctx.repo.insert("institution_audits", {**{key: old[key] for key in keys}, "institution_id": ctx.iid, "logical_audit_id": old["logical_audit_id"], "revision": max(r["revision"] for r in all_revisions) + 1, "supersedes_id": old["id"], "created_by": ctx.actor["id"], "business_status": "DRAFT", "processing_status": "IDLE", "input_hash": old["input_hash"]})
    mappings, remap = [], {}
    sources = {table: await ctx.repo.find(table, {"audit_id": old["id"]}) for table in ("institution_emission_inputs", "institution_mitigation_inputs", "institution_audit_tree_links")}
    for table, rows in sources.items():
        for row in rows:
            remap[str(row["id"])] = str(uuid4())
    def mapped(value):
        if isinstance(value, dict):
            return {key: mapped(item) for key, item in value.items()}
        if isinstance(value, list):
            return [mapped(item) for item in value]
        return remap.get(str(value), value) if isinstance(value, (str, type(old["id"]))) else value
    evidence_ids = set()
    for table, rows in sources.items():
        for row in rows:
            clean = {key: mapped(value) for key, value in row.items() if key not in {"created_at", "updated_at", "input_revision", "created_by", "audit_id"}}
            clean.update(id=remap[str(row["id"])], audit_id=new["id"], created_by=ctx.actor["id"])
            if table in INPUT_KEYS:
                clean["copied_from_id"] = row["id"]
            await ctx.repo.insert(table, clean)
            evidence_ids.update(map(str, row.get("evidence_ids", [])))
            mappings.append({"resource_type": table, "old_id": str(row["id"]), "new_id": clean["id"], "old_local_uuid": None, "new_local_uuid": None})
    links = await ctx.repo.find("institution_evidence_links", {"parent_type": "AUDIT", "parent_id": old["id"]})
    evidence_ids.update(str(link["evidence_id"]) for link in links)
    for declaration in old["coverage"]["categories"]:
        evidence_ids.update(declaration["evidence_ids"])
    await link_evidence(ctx.repo, sorted(evidence_ids), "AUDIT", new["id"], ctx.iid, ctx.actor)
    await ctx.repo.update("institution_audit_periods", family["id"], {"active_draft_audit_id": new["id"]})
    new = await ctx.repo.update("institution_audits", new["id"], {"input_hash": digest(await content(ctx, new))})
    await ctx.repo.event(ctx.actor, "AUDIT_CORRECTION_CREATED", "institution_audits", new, reason=body["reason"])
    ctx.revision, ctx.context_id = 1, None
    output = await resource(ctx, new)
    output["copied_id_mappings"] = mappings
    return output


async def tree_links(ctx, body):
    require_flow()
    audit = ctx.parent
    ctx.match(audit)
    draft(audit, "business_status")
    campuses = set()
    validated = []
    for item in body["items"]:
        tree = await ctx.repo.get("campus_tree_audits", item["tree_audit_id"])
        cid = item["campus_id"]
        if str(tree["institution_id"]) != ctx.iid or str(tree["campus_id"]) != cid or cid not in list(map(str, audit["campus_ids"])):
            fail(404, "RESOURCE_NOT_FOUND", "Campus assessment is unavailable.")
        if cid in campuses or tree["review_status"] != "ACCEPTED" or tree["processing_status"] != "SUCCEEDED":
            fail(422, "INCOMPATIBLE_ASSESSMENT", "Use one independently accepted assessment per included campus.")
        campuses.add(cid)
        comparable = tree["comparability_status"] == "COMPARABLE" and str(tree["comparison_start"]) == str(audit["period_start"]) and str(tree["comparison_end"]) == str(audit["period_end"])
        status = "COMPARABLE" if comparable else "BASELINE_ONLY" if tree["comparability_status"] == "BASELINE_ONLY" else "NONCOMPARABLE"
        if item["comparability_status"] != status or item["period_start"] != str(audit["period_start"]) or item["period_end"] != str(audit["period_end"]):
            fail(422, "INCOMPATIBLE_BASELINE", "Assessment period/comparability does not match the proposed link.")
        campus = await ctx.repo.get("campuses", cid)
        survey = await ctx.repo.get("campus_tree_surveys", tree["survey_id"])
        boundary = await ctx.repo.get("campus_boundaries", tree["boundary_id"])
        mask = await ctx.repo.get("campus_vegetation_masks", tree["mask_id"])
        plots = await ctx.repo.find("campus_sampling_plots", {"plot_set_id": survey["plot_set_id"]})
        validated.append({"institution_id": ctx.iid, "audit_id": audit["id"], "campus_id": cid, "campus_tree_audit_id": tree["id"], "period_start": item["period_start"], "period_end": item["period_end"], "comparability_status": status, "reason": item["reason"], "attributed_removal_tCO2": tree["removal_tCO2"] if comparable else None, "attributed_release_tCO2": tree["release_tCO2"] if comparable else None, "attribution_snapshot": {"assessment": normalize(tree), "campus": normalize(campus), "boundary": normalize(boundary), "mask": normalize(mask), "plots": normalize(plots), "survey": survey["frozen_snapshot"]}, "created_by": ctx.actor["id"]})
    await ctx.repo.conn.execute(text("DELETE FROM institution_audit_tree_links WHERE audit_id=:id"), {"id": audit["id"]})
    for row in validated:
        await ctx.repo.insert("institution_audit_tree_links", row)
    await refresh_hash(ctx, audit)
    return {"items": body["items"], "input_revision": ctx.revision}
