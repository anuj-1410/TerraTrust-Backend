"""Evidence parent authorization independent of object IDs and Storage ACLs."""
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.institution_access import operator_access, REGISTRY_CAPABILITIES, require_flow
from app.institution_errors import fail
from services.institution.base import public, draft
from services.institution.factor_service import REGISTRIES
from services.institution.job_service import enqueue, accepted
from models.institution.common import digest, normalize

PARENTS = {"REGISTRATION": "institution_registrations", "CAMPUS": "campuses", "SURVEY": "campus_tree_surveys", "AUDIT": "institution_audits"}
GLOBALS = {"KNOWLEDGE": "KNOWLEDGE", "MODEL": "MODEL", "FACTOR_SOURCE": "FACTOR", "METHOD_SOURCE": "METHOD"}


def require_upload_configuration(parent_type):
    if parent_type not in GLOBALS:
        require_flow()
        return
    # Operator commissioning must precede selection of an active methodology.
    # The parent authorization separately requires the scoped draft author.
    if not settings.INSTITUTION_SCHEMA_ENABLED or not settings.PRIVATE_STORAGE_BUCKET:
        fail(503, "STORAGE_NOT_CONFIGURED", "Commission the institution schema and private bucket before source authoring.")


async def parent_access(ctx, parent_type, parent_id, *, write=False):
    if parent_type == "REGISTRATION_DRAFT":
        if ctx.actor.get("account_domain") != "INSTITUTION" or str(ctx.actor["id"]) != str(parent_id):
            fail(404, "RESOURCE_NOT_FOUND", "Evidence parent is unavailable.")
        return None
    if parent_type in GLOBALS:
        kind = GLOBALS[parent_type]
        await operator_access(ctx.repo, ctx.actor, REGISTRY_CAPABILITIES[kind])
        row = await ctx.repo.get(REGISTRIES[kind], parent_id)
        if write and (str(row["author_id"]) != str(ctx.actor["id"]) or row["status"] != "DRAFT"):
            fail(403, "PERMISSION_DENIED", "Only the draft's verified author may upload source bytes.")
        return row
    if parent_type not in PARENTS:
        fail(422, "INVALID_PARENT_TYPE", "Unsupported evidence parent.")
    row = await ctx.access(PARENTS[parent_type], parent_id, "W", own_pending=parent_type == "REGISTRATION")
    if write and parent_type in {"REGISTRATION", "SURVEY", "AUDIT"}:
        draft(row, "business_status" if parent_type == "AUDIT" else "status")
    return row


async def evidence_access(ctx, row, *, write=False):
    linked = await ctx.repo.find("institution_evidence_links", {"evidence_id": row["id"]})
    if row["parent_type"] == "REGISTRATION_DRAFT" and linked:
        if write:
            fail(409, "RESOURCE_FROZEN", "Bound registration evidence is immutable.")
        for link in linked:
            try:
                return await parent_access(ctx, link["parent_type"], link["parent_id"])
            except __import__("app.institution_errors", fromlist=["InstitutionError"]).InstitutionError:
                pass
        # Continue only through independent claimed-case authorization below.
        linked_iids = list({str(link["institution_id"]) for link in linked if link["institution_id"]})
        cases = await ctx.repo.rows("SELECT * FROM review_cases WHERE institution_id=ANY(CAST(:ids AS uuid[])) AND status='CLAIMED' AND assigned_reviewer=:uid AND claim_expires_at>now()", {"ids": linked_iids, "uid": ctx.actor["id"]})
        from app.institution_access import independent_reviewer
        for case in cases:
            if str(row["id"]) in list(map(str, case["evidence_ids"])):
                await independent_reviewer(ctx.repo, ctx.actor, case)
                ctx.iid = str(case["institution_id"])
                return case
        fail(404, "RESOURCE_NOT_FOUND", "Evidence is unavailable.")
    try:
        return await parent_access(ctx, row["parent_type"], row["parent_id"], write=write)
    except __import__("app.institution_errors", fromlist=["InstitutionError"]).InstitutionError:
        # Scoped reviewers may read only frozen evidence of their claimed case.
        if write:
            raise
        cases = await ctx.repo.rows("SELECT * FROM review_cases WHERE status='CLAIMED' AND assigned_reviewer=:uid AND claim_expires_at>now()", {"uid": ctx.actor["id"]})
        from app.institution_access import independent_reviewer
        for case in cases:
            if str(row["id"]) in list(map(str, case["evidence_ids"])):
                await independent_reviewer(ctx.repo, ctx.actor, case)
                ctx.iid = str(case["institution_id"])
                return case
        fail(404, "RESOURCE_NOT_FOUND", "Evidence is unavailable.")


async def create_upload(ctx, body):
    require_upload_configuration(body["parent_type"])
    await parent_access(ctx, body["parent_type"], body["parent_id"], write=True)
    if not settings.PRIVATE_STORAGE_BUCKET:
        fail(503, "STORAGE_NOT_CONFIGURED", "The private evidence bucket is not commissioned.")
    mime = body["mime_type"]
    limit = settings.INSTITUTION_UPLOAD_MAX_BYTES
    if mime == "text/csv":
        if body["parent_type"] != "AUDIT":
            fail(422, "UNSUPPORTED_MIME", "CSV evidence is accepted only for an audit import.")
        limit = settings.INSTITUTION_CSV_MAX_BYTES
    elif mime == "application/octet-stream":
        if body["parent_type"] != "MODEL":
            fail(422, "UNSUPPORTED_MIME", "Binary models require a global model source parent.")
        limit = 20 * 1024 * 1024
    elif mime not in {"application/pdf", "image/jpeg", "image/png"}:
        fail(422, "UNSUPPORTED_MIME", "Only PDF, JPEG, PNG and scoped CSV/model files are supported.")
    if not 0 < body["size_bytes"] <= limit:
        fail(413, "FILE_TOO_LARGE", "Evidence exceeds its allowed byte limit.")
    expires = datetime.now(timezone.utc) + timedelta(seconds=settings.INSTITUTION_SIGNED_UPLOAD_TTL_SECONDS)
    if body.get("evidence_id"):
        row = await ctx.repo.get("institution_audit_evidence", body["evidence_id"], lock=True)
        if str(row["owner_user_id"]) != str(ctx.actor["id"]) or str(row["parent_id"]) != body["parent_id"] or any(row[key] != body[key] for key in ("parent_type", "size_bytes", "mime_type", "expected_sha256")):
            fail(404, "RESOURCE_NOT_FOUND", "Upload is unavailable for renewal.")
        if row["state"] not in {"PENDING_UPLOAD", "UPLOADED"} or row["input_revision"] >= 2:
            fail(409, "UPLOAD_NOT_RENEWABLE", "This upload cannot be renewed again.")
        row = await ctx.repo.update("institution_audit_evidence", row["id"], {"expires_at": expires}, bump=True)
    else:
        from uuid import uuid4
        eid = str(uuid4())
        row = await ctx.repo.insert("institution_audit_evidence", {"id": eid, "institution_id": ctx.iid, "owner_user_id": ctx.actor["id"], **{key: body[key] for key in ("parent_type", "parent_id", "file_name", "mime_type", "size_bytes", "expected_sha256")}, "private_bucket": settings.PRIVATE_STORAGE_BUCKET, "object_key": f"quarantine/{ctx.iid or ctx.actor['id']}/{eid}/{body['expected_sha256']}", "state": "PENDING_UPLOAD", "expires_at": expires, "publication_state": "PRIVATE"})
    return {"id": str(row["id"]), "parent_type": row["parent_type"], "parent_id": str(row["parent_id"]), "state": row["state"], "expires_at": normalize(row["expires_at"]), "upload_url": str(ctx.request.base_url).rstrip("/") + "/api/v1/evidence/uploads/" + str(row["id"]) + "/content", "required_headers": {"Content-Type": row["mime_type"]}, "actual_sha256": row["byte_hash"], "size_bytes": row["actual_size_bytes"], "mime_type": row["mime_type"]}


def resource(row):
    return public("EvidenceResource", row, size_bytes=row["actual_size_bytes"] if row["actual_size_bytes"] is not None else row["size_bytes"], bytes_available=row["state"] == "READY" and not row["bytes_deleted_at"])


async def complete(ctx, body):
    row = ctx.evidence
    require_upload_configuration(row["parent_type"])
    if body["parent_id"] != str(row["parent_id"]) or body["expected_sha256"] != row["expected_sha256"]:
        fail(409, "UPLOAD_CONTENT_CHANGED", "Upload parent/hash differs from the selected bytes.")
    if row["state"] not in {"UPLOADED", "CHECKING"}:
        fail(409, "UPLOAD_NOT_COMPLETE", "Upload bytes before requesting verification.")
    job = await enqueue(ctx, "evidence_check", row, {"evidence": normalize(row), "input_hash": digest({"id": str(row["id"]), "sha256": row["expected_sha256"], "parent_id": str(row["parent_id"])})}, asset_type="EVIDENCE")
    await ctx.repo.update("institution_audit_evidence", row["id"], {"state": "CHECKING", "scan_job_id": job["id"]})
    return accepted(job)
