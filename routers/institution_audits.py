"""Annual accounting routes; all edits lock one authorized draft."""
from app.institution_context import endpoint
from app.institution_access import require_flow, RELEASED
from app.institution_errors import fail
from routers.institutions import prepare
from services.institution import annual_service as service
from services.institution.base import draft
from services.institution.factor_service import approved


async def prepare_audit(ctx, mode="R"):
    await ctx.access("institution_audits", ctx.request.path_params["aid"], mode)
    if ctx.member["permission"] == "VIEWER" and ctx.parent["business_status"] not in RELEASED:
        fail(404, "RESOURCE_NOT_FOUND", "Audit is unavailable.")
    if mode == "R" and not __import__("app.config", fromlist=["settings"]).settings.ENABLE_INSTITUTION_FLOW and ctx.parent["business_status"] not in RELEASED:
        fail(503, "INSTITUTION_FLOW_DISABLED", "Only released history is available while institution work is disabled.")


async def writable(ctx):
    await prepare_audit(ctx, "W")


@endpoint("POST", "/institutions/{iid}/audits", model="AuditCreate", status=201, prepare=prepare("institutions", "iid", "W"))
async def create_audit(ctx, body):
    return await service.create(ctx, body)


@endpoint("GET", "/institutions/{iid}/audits", prepare=prepare("institutions", "iid"), query=("business_status",))
async def list_audits(ctx, _):
    async def serialize(row):
        return await service.resource(ctx, row)
    filters = {"institution_id": ctx.iid}
    status = ctx.request.query_params.get("business_status")
    if ctx.member["permission"] == "VIEWER":
        rows = await ctx.repo.find("institution_audits", filters)
        rows = [row for row in rows if row["business_status"] in RELEASED]
        return await ctx.page_rows(rows, serialize)
    if status:
        filters["business_status"] = status
    return await ctx.page("institution_audits", filters, serialize)


@endpoint("GET", "/institution-audits/{aid}", prepare=prepare_audit, query=("revision",))
async def get_audit(ctx, _):
    revision = ctx.request.query_params.get("revision")
    if revision and revision != str(ctx.parent["revision"]):
        fail(409, "AUDIT_REVISION_CONFLICT", "The requested business revision does not match this ID.")
    return await service.resource(ctx, ctx.parent)


@endpoint("GET", "/institution-audits/{aid}/validation", prepare=writable)
async def get_validation(ctx, _):
    return await service.validation(ctx, ctx.parent)


@endpoint("PATCH", "/institution-audits/{aid}", model="AuditPatch", prepare=writable)
async def patch_audit(ctx, body):
    require_flow()
    audit = ctx.parent
    ctx.match(audit)
    draft(audit, "business_status")
    if {"period_start", "period_end", "timezone"} & set(body):
        if audit["revision"] != 1:
            fail(409, "PERIOD_IMMUTABLE", "Correction revisions retain the original period and timezone.")
        period = await ctx.repo.get("institution_audit_periods", audit["logical_audit_id"], lock=True)
        await ctx.repo.update("institution_audit_periods", period["id"], {key: body[key] for key in ("period_start", "period_end", "timezone") if key in body})
    if "method_version" in body:
        await approved(ctx.repo, "carbon_methodologies", body["method_version"])
    if "campus_ids" in body and body["campus_ids"] != list(map(str, audit["campus_ids"])):
        from models.institution.common import normalize
        campuses = []
        if not body["campus_ids"] or len(set(body["campus_ids"])) != len(body["campus_ids"]):
            fail(422, "INVALID_CAMPUS_SELECTION", "Select unique approved campuses.")
        for cid in body["campus_ids"]:
            campus = await ctx.repo.get("campuses", cid)
            if str(campus["institution_id"]) != ctx.iid or campus["control_status"] != "APPROVED" or campus["archived_at"]:
                fail(422, "CAMPUS_NOT_APPROVED", "Selected campuses require current approved control.")
            campuses.append(normalize(campus))
        for table in ("institution_emission_inputs", "institution_mitigation_inputs", "institution_audit_tree_links"):
            if any(r["campus_id"] and str(r["campus_id"]) not in body["campus_ids"] for r in await ctx.repo.find(table, {"audit_id": audit["id"]})):
                fail(409, "CAMPUS_INPUTS_EXIST", "Resolve excluded campus inputs and tree links before changing coverage.")
        body["boundary_snapshot"] = {**audit["boundary_snapshot"], "campuses": campuses}
    audit = await ctx.repo.update("institution_audits", audit["id"], body)
    audit = await service.refresh_hash(ctx, audit)
    return await service.resource(ctx, audit)


@endpoint("PUT", "/institution-audits/{aid}/emission-inputs", model="EmissionReplace", prepare=writable)
async def replace_emissions(ctx, body):
    return await service.write_lines(ctx, body)


@endpoint("POST", "/institution-audits/{aid}/emission-inputs", model="EmissionAppend", status=201, prepare=writable)
async def append_emission(ctx, body):
    return await service.write_lines(ctx, body, append=True)


@endpoint("PATCH", "/institution-audits/{aid}/emission-inputs/{line_id}", prepare=writable)
async def patch_emission(ctx, body):
    return await service.write_lines(ctx, body, patch_id=ctx.request.path_params["line_id"])


@endpoint("DELETE", "/institution-audits/{aid}/emission-inputs/{line_id}", prepare=writable)
async def delete_emission(ctx, _):
    return await service.write_lines(ctx, {}, delete_id=ctx.request.path_params["line_id"])


@endpoint("PUT", "/institution-audits/{aid}/mitigation-inputs", model="MitigationReplace", prepare=writable)
async def replace_mitigations(ctx, body):
    return await service.write_lines(ctx, body, mitigation=True)


@endpoint("POST", "/institution-audits/{aid}/mitigation-inputs", prepare=writable, status=201)
async def append_mitigation(ctx, body):
    if not body or set(body) != {"item"}:
        fail(422, "INVALID_PAYLOAD", "Expected one mitigation item.")
    from models.institution.contracts import parse
    parse("MitigationLine", body["item"])
    return await service.write_lines(ctx, body, mitigation=True, append=True)


@endpoint("PATCH", "/institution-audits/{aid}/mitigation-inputs/{line_id}", prepare=writable)
async def patch_mitigation(ctx, body):
    return await service.write_lines(ctx, body, mitigation=True, patch_id=ctx.request.path_params["line_id"])


@endpoint("DELETE", "/institution-audits/{aid}/mitigation-inputs/{line_id}", prepare=writable)
async def delete_mitigation(ctx, _):
    return await service.write_lines(ctx, {}, mitigation=True, delete_id=ctx.request.path_params["line_id"])


@endpoint("GET", "/institution-audits/{aid}/emission-inputs", prepare=writable, query=("category",))
async def get_emissions(ctx, _):
    category = ctx.request.query_params.get("category")
    if category not in service.CATEGORIES:
        fail(422, "CATEGORY_REQUIRED", "Select a supported category.")
    async def serialize(row):
        return service.line_public("institution_emission_inputs", row)
    page = await ctx.page("institution_emission_inputs", {"audit_id": ctx.parent["id"], "category": category}, serialize)
    page.update(input_revision=ctx.parent["input_revision"], declaration=next(d for d in ctx.parent["coverage"]["categories"] if d["category"] == category))
    return page


@endpoint("GET", "/institution-audits/{aid}/mitigation-inputs", prepare=writable, query=("action",))
async def get_mitigations(ctx, _):
    action = ctx.request.query_params.get("action")
    if action not in {"SOLAR", "RAINWATER", "BIOGAS", "COMPOSTING"}:
        fail(422, "ACTION_REQUIRED", "Select a supported action.")
    async def serialize(row):
        return service.line_public("institution_mitigation_inputs", row)
    page = await ctx.page("institution_mitigation_inputs", {"audit_id": ctx.parent["id"], "action": action}, serialize)
    return {**page, "input_revision": ctx.parent["input_revision"]}


@endpoint("PATCH", "/institution-audits/{aid}/declarations/{category}", model="Declaration", prepare=writable)
async def patch_declaration(ctx, body):
    require_flow()
    audit = ctx.parent
    ctx.match(audit)
    draft(audit, "business_status")
    if body["category"] != ctx.request.path_params["category"]:
        fail(422, "CATEGORY_MISMATCH", "Declaration must match the path category.")
    lines = await ctx.repo.find("institution_emission_inputs", {"audit_id": audit["id"]})
    await service.validate_declaration(ctx, audit, body, lines)
    coverage = {**audit["coverage"], "categories": [body if d["category"] == body["category"] else d for d in audit["coverage"]["categories"]]}
    audit = await ctx.repo.update("institution_audits", audit["id"], {"coverage": coverage})
    await service.refresh_hash(ctx, audit)
    return {"item": body, "input_revision": ctx.revision}


@endpoint("POST", "/institution-audits/{aid}/calculate", model="CalculateRequest", status=202, prepare=writable)
async def calculate_audit(ctx, body):
    return await service.calculate(ctx, body)


@endpoint("POST", "/institution-audits/{aid}/submit", model="SubmitRequest", prepare=writable)
async def submit_audit(ctx, body):
    return await service.calculate(ctx, body, submit=True)


@endpoint("POST", "/institution-audits/{aid}/revisions", model="RevisionCreate", status=201, prepare=writable)
async def revise_audit(ctx, body):
    return await service.revision(ctx, body)


@endpoint("GET", "/institution-audits/{aid}/revisions", prepare=prepare_audit)
async def get_revisions(ctx, _):
    rows = await ctx.repo.find("institution_audits", {"logical_audit_id": ctx.parent["logical_audit_id"]})
    if ctx.member["permission"] == "VIEWER":
        rows = [r for r in rows if r["business_status"] in RELEASED]
    return await ctx.page_rows(rows, lambda row: service.resource(ctx, row))


@endpoint("GET", "/institution-audits/{aid}/result", prepare=prepare_audit, query=("snapshot_id",))
async def get_result(ctx, _):
    return await service.result(ctx, ctx.parent, ctx.request.query_params.get("snapshot_id"))


@endpoint("PUT", "/institution-audits/{aid}/tree-links", model="TreeLinksReplace", prepare=writable)
async def replace_tree_links(ctx, body):
    return await service.tree_links(ctx, body)


@endpoint("GET", "/institution-audits/{aid}/tree-links", prepare=prepare_audit)
async def get_tree_links(ctx, _):
    rows = await ctx.repo.find("institution_audit_tree_links", {"audit_id": ctx.parent["id"]})
    return {"links": [{"campus_id": str(r["campus_id"]), "tree_audit_id": str(r["campus_tree_audit_id"]), **{key: r[key] for key in ("period_start", "period_end", "comparability_status", "reason")}} for r in rows], "input_revision": ctx.parent["input_revision"]}
