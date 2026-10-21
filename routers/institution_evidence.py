"""Private evidence and a five-minute authenticated streaming upload lease."""
import hashlib
from datetime import datetime, timezone

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.config import settings
from app.institution_context import endpoint, router, Context, openapi_bearer
from app.institution_access import identity, user_for_claims
from app.institution_errors import fail
from models.institution.common import normalize
from repositories.institution.base import Repository
from services.institution import evidence_service as service
from services.shared import evidence_service as storage


async def prepare_upload(ctx):
    body = ctx.body or {}
    if "parent_type" in body and "parent_id" in body:
        await service.parent_access(ctx, body["parent_type"], body["parent_id"])


async def prepare_evidence(ctx):
    row = await ctx.repo.get("institution_audit_evidence", ctx.request.path_params["eid"])
    await service.evidence_access(ctx, row)
    ctx.evidence = row


@endpoint("POST", "/evidence/uploads", model="UploadCreate", status=201, prepare=prepare_upload)
async def create_upload(ctx, body):
    return await service.create_upload(ctx, body)


@endpoint("POST", "/evidence/uploads/{eid}/complete", model="UploadComplete", status=202, prepare=prepare_evidence)
async def complete_upload(ctx, body):
    await service.parent_access(ctx, ctx.evidence["parent_type"], ctx.evidence["parent_id"], write=True)
    return await service.complete(ctx, body)


@endpoint("GET", "/evidence/{eid}", prepare=prepare_evidence)
async def get_evidence(ctx, _):
    return service.resource(ctx.evidence)


@endpoint("GET", "/evidence/{eid}/download", prepare=prepare_evidence)
async def download_evidence(ctx, _):
    return await storage.download(ctx.evidence)


@endpoint("GET", "/evidence", query=("parent_type", "parent_id"))
async def list_evidence(ctx, _):
    parent_type, parent_id = ctx.request.query_params.get("parent_type"), ctx.request.query_params.get("parent_id")
    if not parent_type or not parent_id:
        fail(422, "PARENT_REQUIRED", "Select one authorized evidence parent.")
    await service.parent_access(ctx, parent_type, parent_id)
    async def serialize(row):
        return service.resource(row)
    # Direct objects and explicit immutable correction links share one list.
    linked = await ctx.repo.rows("SELECT e.* FROM institution_audit_evidence e WHERE (e.parent_type=:type AND e.parent_id=:id) OR EXISTS(SELECT 1 FROM institution_evidence_links l WHERE l.evidence_id=e.id AND l.parent_type=:type AND l.parent_id=:id) ORDER BY e.created_at DESC,e.id DESC", {"type": parent_type, "id": parent_id})
    return await ctx.page_rows(linked, serialize)


async def _upload_content(eid: str, request: Request):
    """Transport bytes only; authorization and lease replace a long storage token."""
    if not settings.INSTITUTION_SCHEMA_ENABLED:
        fail(503, "INSTITUTION_FLOW_DISABLED", "Institution storage is not commissioned.")
    from models.institution.common import uuid_text
    try:
        uuid_text(eid)
    except (ValueError, TypeError):
        fail(422,"INVALID_RESOURCE_ID","Upload identifier must be a canonical UUID.")
    claims = await identity(request.headers.get("Authorization"))
    from app.institution_database import _require_async_engine
    async with _require_async_engine().begin() as conn:
        repo = Repository(conn)
        actor = await user_for_claims(repo, claims, "UPDATE")
        ctx = Context(request, repo, actor, claims)
        await ctx.check_generation()
        row = await repo.get("institution_audit_evidence", eid)
        await service.evidence_access(ctx, row)
        service.require_upload_configuration(row["parent_type"])
        row = await repo.get("institution_audit_evidence",eid,lock=True)
        if str(row["owner_user_id"]) != str(actor["id"]):
            fail(404, "RESOURCE_NOT_FOUND", "Upload lease is unavailable.")
        await ctx.rate_limit()
        collected = bytearray()
        async for chunk in request.stream():
            if len(collected) + len(chunk) > row["size_bytes"]:
                fail(413, "FILE_TOO_LARGE", "Uploaded bytes exceed the declared size.")
            collected.extend(chunk)
        data = bytes(collected)
        actual_hash = hashlib.sha256(data).hexdigest()
        replay = await ctx.receipt({"binary_sha256": actual_hash, "size_bytes": len(data), "mime_type": request.headers.get("Content-Type")})
        if replay:
            return JSONResponse(status_code=replay[0], content=replay[1], headers={"Cache-Control": "private, no-store"})
        await service.parent_access(ctx, row["parent_type"], row["parent_id"], write=True)
        if row["state"] not in {"PENDING_UPLOAD", "UPLOADED"} or row["expires_at"] <= datetime.now(timezone.utc):
            fail(409, "UPLOAD_LEASE_EXPIRED", "Renew the upload lease before sending these bytes.")
        if len(data) != row["size_bytes"] or actual_hash != row["expected_sha256"] or request.headers.get("Content-Type") != row["mime_type"]:
            fail(422, "UPLOAD_INTEGRITY_MISMATCH", "Uploaded byte size/hash/MIME differs from the selected file.")
        await storage.write_bytes(row["private_bucket"], row["object_key"], data, row["mime_type"])
        row = await repo.update("institution_audit_evidence", eid, {"state": "UPLOADED"})
        await ctx.check_generation()
        await user_for_claims(repo,claims)
        result = ctx.success(service.resource(row))
        await ctx.save_receipt(200, result)
    return JSONResponse(content=result, headers={"Cache-Control": "private, no-store"})


@router.put("/evidence/uploads/{eid}/content", tags=["Institution"], name="upload_private_evidence_bytes", dependencies=[Depends(openapi_bearer)], openapi_extra={
    "parameters": [{"name": "Idempotency-Key", "in": "header", "required": True, "schema": {"type": "string"}}, {"name": "X-TerraTrust-Demo-Generation", "in": "header", "required": False, "schema": {"type": "integer", "minimum": 1}}],
    "requestBody": {"required": True, "description": "Exact selected bytes and Content-Type from the upload lease.", "content": {mime: {"schema": {"type": "string", "format": "binary"}} for mime in ("application/pdf", "image/jpeg", "image/png", "text/csv", "application/octet-stream")}},
})
async def upload_content(eid: str, request: Request):
    try:
        return await _upload_content(eid, request)
    except IntegrityError:
        fail(409, "CONSTRAINT_CONFLICT", "Upload conflicts with an accepted record.")
    except SQLAlchemyError:
        fail(503, "DATABASE_UNAVAILABLE", "Institution storage is temporarily unavailable.", retryable=True)
