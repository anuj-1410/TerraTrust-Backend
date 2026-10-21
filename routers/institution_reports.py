"""Exact-snapshot reports, byte authorization, CSV imports and analytics."""
import csv
import io
from uuid import uuid4

from app.institution_context import endpoint
from app.institution_access import require_flow
from app.institution_errors import fail
from models.institution.common import normalize, digest
from routers.institutions import prepare
from routers.institution_audits import prepare_audit, writable
from services.institution.base import public
from services.institution.job_service import enqueue, accepted
from services.institution import annual_service, import_service, report_service, analytics_service
from services.shared.evidence_service import download


async def export_resource(ctx, row):
    artifact = await ctx.repo.get("institution_artifacts", row["artifact_id"]) if row["artifact_id"] else None
    return public("ExportResource", row, artifact_id=str(row["artifact_id"]) if row["artifact_id"] else None, download_artifact_id=str(row["artifact_id"]) if row["artifact_id"] else None, byte_hash=artifact["byte_hash"] if artifact else None, size_bytes=artifact["size_bytes"] if artifact else None, bytes_available=artifact is not None and artifact["status"] == "READY" and not artifact["bytes_deleted_at"], bytes_deleted_at=artifact["bytes_deleted_at"] if artifact else None, template_version=report_service.TEMPLATE_VERSION)


@endpoint("POST", "/institution-audits/{aid}/exports", model="ExportCreate", status=202, prepare=writable)
async def create_export(ctx, body):
    require_flow()
    audit = ctx.parent
    result = await annual_service.result(ctx, audit, body["snapshot_id"])
    if result["result_hash"] != body["expected_result_hash"]:
        fail(409, "RESULT_HASH_CHANGED", "Select the exact result hash to export.")
    snapshot = await ctx.repo.get("institution_calculation_snapshots", body["snapshot_id"])
    advice = []
    if body.get("include_advice", False):
        from services.advisory.advisory_service import resource as advice_resource
        rows = await ctx.repo.find("advisory_queries", {"audit_id": audit["id"], "snapshot_id": snapshot["id"], "publication_state": "PUBLISHED"})
        advice = [advice_resource(row) for row in rows if row["answer_hash"] and row["context_hash"] == result["result_hash"]]
    draft = audit["business_status"] not in {"FINALIZED", "SUPERSEDED"} or str(audit["reviewed_snapshot_id"]) != body["snapshot_id"]
    export_id, jid = str(uuid4()), str(uuid4())
    row = await ctx.repo.insert("institution_exports", {"id": export_id, "institution_id": ctx.iid, "audit_id": audit["id"], "snapshot_id": snapshot["id"], "result_hash": result["result_hash"], "format": body["format"], "job_id": jid, "status": "QUEUED", "draft_watermark": draft, "include_advice": bool(body.get("include_advice")), "advice_query_ids": [a["query_id"] for a in advice], "advice_hashes": [a["answer_hash"] for a in advice], "created_by": ctx.actor["id"]})
    manifest = {"schema_version": 1, "template_version": report_service.TEMPLATE_VERSION, "result": result, "frozen_input": snapshot["frozen_input"], "draft_watermark": draft, "data_origin": audit["data_origin"], "demo": ctx.generation is not None, "advice": advice, "review": {"reviewed_by": normalize(audit["reviewed_by"]), "finalized_by": normalize(audit["finalized_by"]), "reviewed_at": normalize(audit["reviewed_at"]), "finalized_at": normalize(audit["finalized_at"])}}
    manifest["warnings"] = []
    for eid in snapshot["frozen_input"]["evidence_manifest"]:
        item = await ctx.repo.get("institution_audit_evidence",eid)
        if item["bytes_deleted_at"]:
            manifest["warnings"].append("SOURCE_BYTES_UNAVAILABLE: "+str(eid))
    job = await enqueue(ctx, "report_export", row, {"manifest": manifest, "format": body["format"], "input_hash": digest({"snapshot_id": body["snapshot_id"], "result_hash": result["result_hash"], "format": body["format"], "template_version": report_service.TEMPLATE_VERSION, "advice_hashes": row["advice_hashes"]})}, asset_type="REPORT", job_id=jid)
    return accepted(job)


@endpoint("GET", "/institution-audits/{aid}/exports", prepare=prepare_audit, query=("snapshot_id", "format"))
async def list_exports(ctx, _):
    rows = await ctx.repo.find("institution_exports", {"audit_id": ctx.parent["id"]})
    if ctx.member["permission"] == "VIEWER":
        rows = [r for r in rows if not r["draft_watermark"] and str(r["snapshot_id"]) == str(ctx.parent["reviewed_snapshot_id"])]
    if ctx.request.query_params.get("snapshot_id"):
        rows = [r for r in rows if str(r["snapshot_id"]) == ctx.request.query_params["snapshot_id"]]
    if ctx.request.query_params.get("format"):
        rows = [r for r in rows if r["format"] == ctx.request.query_params["format"]]
    return await ctx.page_rows(rows, lambda row: export_resource(ctx, row))


async def member_artifact_access(ctx):
    row = await ctx.repo.get("institution_artifacts", ctx.request.path_params["artifact_id"])
    parent = row["parent_type"]
    if parent == "REPORT":
        export = await ctx.access("institution_exports", row["parent_id"])
        audit = await ctx.repo.get("institution_audits", export["audit_id"])
        if ctx.member["permission"] == "VIEWER" and (row["draft"] or audit["business_status"] not in {"FINALIZED", "SUPERSEDED"} or str(audit["reviewed_snapshot_id"]) != str(export["snapshot_id"])):
            fail(404, "RESOURCE_NOT_FOUND", "Report is unavailable.")
        for qid in export["advice_query_ids"]:
            query = await ctx.repo.get("advisory_queries", qid)
            if query["publication_state"] != "PUBLISHED":
                fail(409, "ADVICE_PUBLICATION_CHANGED", "Included advice was withdrawn; create a newly reviewed report.")
    elif parent == "IMPORT_TEMPLATE":
        await ctx.access("institution_audits", row["parent_id"], "W")
    elif parent in {"MASK", "TREE_AUDIT", "PLOT_MAP"}:
        table = {"MASK": "campus_vegetation_masks", "TREE_AUDIT": "campus_tree_audits", "PLOT_MAP": "campus_plot_sets"}[parent]
        resource = await ctx.access(table, row["parent_id"])
        if ctx.member["permission"] == "VIEWER":
            from services.institution.visibility import released_snapshot
            snapshot = await released_snapshot(ctx, str(row["snapshot_id"]) if row["snapshot_id"] else None)
            if not any(str(link["assessment"]["id"]) == str(resource["id"]) or str(link["assessment"]["mask_id"]) == str(resource["id"]) for link in snapshot["frozen_input"]["tree_links"]):
                fail(404, "RESOURCE_NOT_FOUND", "Map is unavailable in this released snapshot.")
    else:
        fail(404, "RESOURCE_NOT_FOUND", "Artifact is unavailable.")
    ctx.artifact = row


async def artifact_access(ctx):
    from app.institution_errors import InstitutionError
    try:
        return await member_artifact_access(ctx)
    except InstitutionError as exc:
        if exc.status not in {403, 404}:
            raise
        row = await ctx.repo.get("institution_artifacts",ctx.request.path_params["artifact_id"])
        if row["parent_type"] == "REPORT":
            export = await ctx.repo.get("institution_exports",row["parent_id"])
            source_id = export["audit_id"]
            snapshot = await ctx.repo.get("institution_calculation_snapshots",export["snapshot_id"])
        elif row["parent_type"] in {"MASK","TREE_AUDIT","PLOT_MAP"}:
            source_id,snapshot = row["parent_id"],None
        else:
            fail(404,"RESOURCE_NOT_FOUND","Artifact is unavailable.")
        from services.institution.reviewer_access import claimed_resource
        case = await claimed_resource(ctx,row["institution_id"],source_id)
        if snapshot and (snapshot["input_hash"] != case["input_hash"] or snapshot["result_hash"] != export["result_hash"]):
            fail(404,"RESOURCE_NOT_FOUND","This report is outside the claimed frozen review.")
        ctx.artifact = row


@endpoint("GET", "/artifacts/{artifact_id}/download", prepare=artifact_access, query=("snapshot_id",))
async def download_artifact(ctx, _):
    return await download(ctx.artifact, artifact=True)


@endpoint("GET", "/institutions/{iid}/analytics", prepare=prepare("institutions", "iid"), query=("audit_ids", "period_start", "period_end", "campus_ids", "scope", "category", "denominator", "metric"))
async def analytics(ctx, _):
    return await analytics_service.analytics(ctx)


@endpoint("POST", "/institution-audits/{aid}/imports/preview", model="ImportPreviewRequest", prepare=writable)
async def import_preview(ctx, body):
    return await import_service.preview(ctx, body)


@endpoint("POST", "/institution-audits/{aid}/imports/commit", model="ImportCommitRequest", prepare=writable)
async def import_commit(ctx, body):
    return await import_service.commit(ctx, body)


@endpoint("GET", "/institution-audits/{aid}/imports/{batch_id}", prepare=writable, query=("errors_only",))
async def import_status(ctx, _):
    batch = await ctx.repo.get("institution_import_batches", ctx.request.path_params["batch_id"])
    return await import_service.import_resource(ctx, ctx.parent, batch)


@endpoint("GET", "/institution-audits/{aid}/imports/template", prepare=writable, query=("category", "schema_version"))
async def import_template(ctx, _):
    category = ctx.request.query_params.get("category")
    if category not in annual_service.CATEGORIES or ctx.request.query_params.get("schema_version", "1") != "1":
        fail(422, "INVALID_TEMPLATE", "Select a supported category and schema_version=1.")
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\r\n").writerow(import_service.COLUMNS)
    artifact = await report_service.artifact(ctx.repo, ctx.iid, "IMPORT_TEMPLATE", ctx.parent["id"], output.getvalue().encode("utf-8-sig"), "text/csv", draft=True)
    return {"artifact_id": str(artifact["id"]), "schema_version": 1, "category": category, "download": await download(artifact, artifact=True)}


@endpoint("GET", "/institution-audits/{aid}/evidence", prepare=writable)
async def audit_evidence(ctx, _):
    from services.institution.evidence_service import resource
    async def serialize(row):
        return resource(row)
    return await ctx.page("institution_audit_evidence", {"parent_type": "AUDIT", "parent_id": ctx.parent["id"]}, serialize)
