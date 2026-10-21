"""Stable inventory, field survey synchronization and institution-only tree jobs."""
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import text

from app.institution_context import endpoint
from app.institution_access import require_flow
from app.institution_errors import fail
from models.institution.common import measurement_hash, normalize
from models.institution.contracts import parse
from routers.institutions import prepare
from services.institution import survey_service as service
from services.institution.base import public, draft, link_evidence


@endpoint("POST", "/campuses/{cid}/trees", model="TreeCreate", status=201, prepare=prepare("campuses", "cid", "W"))
async def create_tree(ctx, body):
    return await service.create_tree(ctx, body)


@endpoint("PATCH", "/campuses/{cid}/trees/{tid}", model="TreePatch", prepare=prepare("campuses", "cid", "W"))
async def patch_tree(ctx, body):
    return await service.patch_tree(ctx, body)


@endpoint("GET", "/campuses/{cid}/trees", prepare=prepare("campuses", "cid"), query=("search", "snapshot_id", "sort"))
async def trees(ctx, _):
    if ctx.member["permission"] == "VIEWER":
        from services.institution.visibility import released_snapshot
        snapshot = await released_snapshot(ctx)
        rows = [tree for link in snapshot["frozen_input"]["tree_links"] if str(link["campus_id"]) == str(ctx.parent["id"]) for tree in link["attribution_snapshot"]["survey"]["trees"]]
    else:
        rows = await ctx.repo.find("campus_trees", {"campus_id": ctx.parent["id"]})
    params = ctx.request.query_params
    search = params.get("search", "").strip().casefold()
    if search:
        rows = [row for row in rows if any(search in str(row.get(key) or "").casefold() for key in ("stable_tag", "species_code"))]
    order = params.get("sort", "stable_tag")
    if order not in {"stable_tag", "-stable_tag", "created_at", "-created_at"}:
        fail(422, "INVALID_SORT", "Tree sort supports stable_tag or created_at, optionally prefixed with '-'.")
    rows = sorted(rows, key=lambda row: (str(row.get(order.lstrip("-")) or "").casefold(), str(row["id"])), reverse=order.startswith("-"))
    return await ctx.page_rows(rows, lambda row: public("TreeResource", row))


@endpoint("GET", "/campuses/{cid}/trees/{tid}", prepare=prepare("campuses", "cid"), query=("snapshot_id",))
async def tree(ctx, _):
    if ctx.member["permission"] == "VIEWER":
        from services.institution.visibility import released_resource
        return await released_resource(ctx, "tree", ctx.request.path_params["tid"])
    row = await ctx.repo.get("campus_trees", ctx.request.path_params["tid"])
    if str(row["campus_id"]) != str(ctx.parent["id"]):
        fail(404, "RESOURCE_NOT_FOUND", "Tree is unavailable.")
    return public("TreeResource", row)


@endpoint("GET", "/campuses/{cid}/trees/{tid}/events", prepare=prepare("campuses", "cid"), query=("snapshot_id",))
async def tree_events(ctx, _):
    tid = ctx.request.path_params["tid"]
    if ctx.member["permission"] == "VIEWER":
        from services.institution.visibility import released_snapshot
        snapshot = await released_snapshot(ctx)
        rows = [event for link in snapshot["frozen_input"]["tree_links"] if str(link["campus_id"]) == str(ctx.parent["id"]) for event in link["attribution_snapshot"]["assessment"]["provenance"].get("events", []) if str(event["tree_id"]) == tid]
        return await ctx.page_rows(rows, lambda row: public("TreeEventResource", row, type=row["event_type"], date=row["event_date"]))
    row = await ctx.repo.get("campus_trees", tid)
    if str(row["campus_id"]) != str(ctx.parent["id"]):
        fail(404, "RESOURCE_NOT_FOUND", "Tree is unavailable.")
    async def serialize(event):
        return public("TreeEventResource", event, type=event["event_type"], date=event["event_date"])
    return await ctx.page("campus_tree_events", {"tree_id": tid, "campus_id": ctx.parent["id"]}, serialize)


@endpoint("POST", "/campuses/{cid}/tree-surveys", model="SurveyCreate", status=201, prepare=prepare("campuses", "cid", "W"))
async def create_survey(ctx, body):
    return await service.create_survey(ctx, body)


@endpoint("GET", "/campuses/{cid}/tree-surveys", prepare=prepare("campuses", "cid", "W"), query=("status",))
async def surveys(ctx, _):
    async def serialize(row):
        return await service.survey_resource(ctx, row)
    filters = {"campus_id": ctx.parent["id"]}
    if ctx.request.query_params.get("status"):
        filters["status"] = ctx.request.query_params["status"]
    return await ctx.page("campus_tree_surveys", filters, serialize)


@endpoint("GET", "/tree-surveys/{sid}", prepare=prepare("campus_tree_surveys", "sid", "W"))
async def get_survey(ctx, _):
    return await service.survey_resource(ctx, ctx.parent)


@endpoint("POST", "/tree-surveys/{sid}/measurements/batch", model="MeasurementBatch", prepare=prepare("campus_tree_surveys", "sid", "W"))
async def measurement_batch(ctx, body):
    return await service.batch(ctx, body)


@endpoint("GET", "/tree-surveys/{sid}/measurements", prepare=prepare("campus_tree_surveys", "sid", "W"), query=("plot_id", "since_input_revision"))
async def measurements(ctx, _):
    filters = {"survey_id": ctx.parent["id"], "deleted_at": None}
    if ctx.request.query_params.get("plot_id"):
        filters["plot_id"] = ctx.request.query_params["plot_id"]
    async def serialize(row):
        return service.measurement_resource(row)
    if "since_input_revision" in ctx.request.query_params:
        since = int(ctx.request.query_params["since_input_revision"])
        if since > ctx.parent["input_revision"]:
            fail(409,"SURVEY_REVISION_CONFLICT","Delta cursor is newer than the saved survey.")
        filters.pop("deleted_at")
        rows = [r for r in await ctx.repo.find("campus_tree_measurements",filters) if r["last_survey_input_revision"]>since]
        ctx.revision = ctx.parent["input_revision"]
        return await ctx.page_rows(rows,lambda row:{**service.measurement_resource(row),"deleted_at":normalize(row["deleted_at"]),"last_survey_input_revision":row["last_survey_input_revision"]})
    return await ctx.page("campus_tree_measurements", filters, serialize)


async def mutate_measurement(ctx, body, *, delete=False):
    require_flow()
    survey = ctx.parent
    ctx.match(survey)
    draft(survey)
    row = await ctx.repo.get("campus_tree_measurements", ctx.request.path_params["mid"], lock=True)
    if str(row["survey_id"]) != str(survey["id"]) or row["deleted_at"]:
        fail(404, "RESOURCE_NOT_FOUND", "Observation is unavailable.")
    if delete:
        await ctx.repo.update("campus_tree_measurements", row["id"], {"deleted_at": datetime.now(timezone.utc),"last_survey_input_revision":survey["input_revision"]+1}, bump=True)
    else:
        if not row["raw_input"]:
            fail(409, "RAW_CAPTURE_UNAVAILABLE", "Original capture bytes are unavailable; a resurvey is required.")
        if body["client_operation_id"] != ctx.request.headers["Idempotency-Key"]:
            fail(409, "OPERATION_ID_MISMATCH", "Mutation ID must equal Idempotency-Key.")
        raw = {**row["raw_input"], **{key: value for key, value in body.items() if key not in {"client_operation_id", "client_content_hash"}}}
        parse("MeasurementItem", {**raw, "client_content_hash": body["client_content_hash"]})
        diameter, issues = await service.observation_quality(ctx, survey, raw)
        saved = await ctx.repo.update("campus_tree_measurements", row["id"], {**raw, "raw_input": raw, "content_hash": body["client_content_hash"], "derived_dbh_cm": normalize(diameter) if raw["measurement_form"] == "CIRCUMFERENCE" else None, "validation_status": "NEEDS_DATA" if issues else "VALID", "validation_issues": issues, "revision": row["revision"] + 1}, bump=True)
        await ctx.repo.update("campus_tree_measurements",row["id"],{"last_survey_input_revision":survey["input_revision"]+1})
    await service.survey_hash(ctx, survey)
    return {"deleted_id": str(row["id"]), "input_revision": ctx.revision} if delete else {"item": service.measurement_resource(saved), "input_revision": ctx.revision}


@endpoint("PUT", "/tree-surveys/{sid}/measurements/{mid}", model="MeasurementPatch", prepare=prepare("campus_tree_surveys", "sid", "W"))
async def patch_measurement(ctx, body):
    return await mutate_measurement(ctx, body)


@endpoint("DELETE", "/tree-surveys/{sid}/measurements/{mid}", prepare=prepare("campus_tree_surveys", "sid", "W"))
async def delete_measurement(ctx, _):
    return await mutate_measurement(ctx, {}, delete=True)


@endpoint("POST", "/tree-surveys/{sid}/submit", model="SubmitRequest", prepare=prepare("campus_tree_surveys", "sid", "W"))
async def submit_survey(ctx, body):
    return await service.submit(ctx, body)


@endpoint("POST", "/tree-surveys/{sid}/revisions", model="RevisionCreate", status=201, prepare=prepare("campus_tree_surveys", "sid", "W"))
async def revise_survey(ctx, body):
    require_flow()
    old = ctx.parent
    ctx.match(old)
    if old["status"] not in {"NEEDS_CORRECTION", "ACCEPTED"} or body["source_input_revision"] != old["input_revision"]:
        fail(409, "REVIEW_IN_PROGRESS", "This survey is not ready for a correction revision.")
    rows = await ctx.repo.find("campus_tree_surveys", {"logical_survey_id": old["logical_survey_id"]})
    if any(row["status"] in {"DRAFT", "SUBMITTED", "UNDER_REVIEW"} for row in rows):
        fail(409, "REVIEW_IN_PROGRESS", "One working survey revision already exists.")
    new = await ctx.repo.insert("campus_tree_surveys", {**{key: old[key] for key in ("institution_id", "campus_id", "logical_survey_id", "boundary_id", "mask_id", "plot_set_id", "quality_policy_version", "survey_start", "survey_end")}, "revision": max(r["revision"] for r in rows) + 1, "created_by": ctx.actor["id"], "status": "DRAFT", "input_hash": old["input_hash"], "supersedes_id": old["id"], "sync_validation": {}})
    mappings, evidence_ids = [], set()
    for obs in await ctx.repo.find("campus_tree_measurements", {"survey_id": old["id"], "deleted_at": None}):
        if not obs["raw_input"]:
            fail(409, "RAW_CAPTURE_UNAVAILABLE", "Original captures are required to clone a correction.")
        raw = {**obs["raw_input"], "local_uuid": str(uuid4())}
        values = {key: value for key, value in obs.items() if key not in {"id", "created_at", "updated_at", "input_revision"}}
        values.update(survey_id=new["id"], last_survey_input_revision=1, revision=1, local_uuid=raw["local_uuid"], raw_input=raw, content_hash=measurement_hash(raw), copied_from_id=obs["id"], created_by=ctx.actor["id"])
        saved = await ctx.repo.insert("campus_tree_measurements", values)
        evidence_ids.update(map(str, obs["evidence_ids"]))
        mappings.append({"resource_type": "MEASUREMENT", "old_id": str(obs["id"]), "new_id": str(saved["id"]), "old_local_uuid": str(obs["local_uuid"]), "new_local_uuid": raw["local_uuid"]})
    await link_evidence(ctx.repo, sorted(evidence_ids), "SURVEY", new["id"], ctx.iid, ctx.actor)
    new = await service.survey_hash(ctx, new)
    output = await service.survey_resource(ctx, new)
    output["copied_id_mappings"] = mappings
    return output


@endpoint("POST", "/campuses/{cid}/tree-audits", model="TreeAuditCreate", status=202, prepare=prepare("campuses", "cid", "W"))
async def request_tree_audit(ctx, body):
    return await service.request_audit(ctx, body)


@endpoint("GET", "/campus-tree-audits/{taid}", prepare=prepare("campus_tree_audits", "taid"), query=("snapshot_id",))
async def get_tree_audit(ctx, _):
    if ctx.member["permission"] == "VIEWER":
        from services.institution.visibility import released_resource
        return await released_resource(ctx, "tree_audit", ctx.parent["id"])
    return service.tree_audit_resource(ctx.parent)


@endpoint("GET", "/campuses/{cid}/tree-audits", prepare=prepare("campuses", "cid"), query=("snapshot_id", "review_status"))
async def tree_audits(ctx, _):
    if ctx.member["permission"] == "VIEWER":
        from services.institution.visibility import released_snapshot
        snapshots = [await released_snapshot(ctx)] if ctx.request.query_params.get("snapshot_id") else await ctx.repo.rows("SELECT s.frozen_input FROM institution_calculation_snapshots s JOIN institution_audits a ON a.reviewed_snapshot_id=s.id WHERE a.institution_id=:iid AND a.business_status IN ('FINALIZED','SUPERSEDED') ORDER BY a.period_end DESC,a.revision DESC,a.id DESC", {"iid": ctx.iid})
        rows = {str(link["assessment"]["id"]): link["assessment"] for snap in snapshots for link in snap["frozen_input"]["tree_links"] if str(link["campus_id"]) == str(ctx.parent["id"])}
        return await ctx.page_rows(list(rows.values()), service.tree_audit_resource)
    async def serialize(row):
        return service.tree_audit_resource(row)
    filters = {"campus_id": ctx.parent["id"]}
    if ctx.request.query_params.get("review_status"):
        filters["review_status"] = ctx.request.query_params["review_status"]
    return await ctx.page("campus_tree_audits", filters, serialize)
