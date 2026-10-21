"""Profile, registration, invitation and membership HTTP boundaries."""
from datetime import datetime, timezone

from app.config import settings
from app.institution_access import require_flow
from app.institution_context import endpoint
from app.institution_errors import fail
from services.institution import institution_service as service
from services.institution.base import public


def prepare(table, param, mode="R", own_pending=False):
    async def load(ctx):
        await ctx.access(table, ctx.request.path_params[param], mode, own_pending=own_pending)
    return load


@endpoint("POST", "/institutions", model="InstitutionCreate", status=201)
async def create_institution(ctx, body):
    return await service.create(ctx, body)


@endpoint("GET", "/institutions")
async def list_institutions(ctx, _):
    rows = await ctx.repo.rows("SELECT DISTINCT i.* FROM institutions i LEFT JOIN institution_users iu ON iu.institution_id=i.id WHERE (iu.user_id=:uid AND iu.status='ACTIVE') OR (i.created_by=:uid AND i.verification_status IN ('PENDING','REJECTED')) ORDER BY i.created_at DESC,i.id DESC", {"uid": ctx.actor["id"]})
    if not settings.ENABLE_INSTITUTION_FLOW:
        rows = [row for row in rows if row["verification_status"] == "APPROVED"]
    return await ctx.page_rows(rows, lambda row: service.institution_resource(ctx, row))


@endpoint("GET", "/institutions/{iid}", prepare=prepare("institutions", "iid", own_pending=True), query=("snapshot_id",))
async def get_institution(ctx, _):
    if ctx.member and ctx.member["permission"] == "VIEWER":
        from services.institution.visibility import released_resource
        return await released_resource(ctx, "institution", ctx.iid)
    return await service.institution_resource(ctx, ctx.parent)


@endpoint("PATCH", "/institutions/{iid}", model="InstitutionPatch", prepare=prepare("institutions", "iid", "O", True))
async def patch_institution(ctx, body):
    return await service.patch(ctx, body)


@endpoint("GET", "/institutions/{iid}/registration", prepare=prepare("institutions", "iid", "O", True), query=("registration_id",))
async def get_registration(ctx, _):
    reg_id = ctx.request.query_params.get("registration_id") or ctx.parent["current_registration_id"]
    reg = await ctx.repo.get("institution_registrations", reg_id)
    if str(reg["institution_id"]) != ctx.iid:
        fail(404, "RESOURCE_NOT_FOUND", "Registration is unavailable.")
    return await service.registration_resource(ctx, reg)


@endpoint("POST", "/institutions/{iid}/submit-registration", model="SubmitRequest", prepare=prepare("institutions", "iid", "O", True))
async def submit_registration(ctx, body):
    return await service.submit_registration(ctx, body)


@endpoint("POST", "/institutions/{iid}/registration/revisions", model="RevisionCreate", status=201, prepare=prepare("institutions", "iid", "O", True))
async def correct_registration(ctx, body):
    return await service.registration_revision(ctx, body)


@endpoint("GET", "/institutions/{iid}/members", prepare=prepare("institutions", "iid", "O"))
async def list_members(ctx, _):
    async def serialize(row):
        user = await ctx.repo.one("SELECT full_name FROM users WHERE id=:id", {"id": row["user_id"]})
        return public("MembershipResource", row, display_name=user["full_name"])
    return await ctx.page("institution_users", {"institution_id": ctx.iid}, serialize)


@endpoint("POST", "/institutions/{iid}/members", model="InviteCreate", status=201, prepare=prepare("institutions", "iid", "O"))
async def invite_member(ctx, body):
    return await service.invite(ctx, body)


@endpoint("PATCH", "/institutions/{iid}/members/{uid}", model="MemberPatch", prepare=prepare("institutions", "iid", "O"))
async def patch_member(ctx, body):
    return await service.member_patch(ctx, body)


async def prepare_accept(ctx):
    import hashlib
    hashed = hashlib.sha256(ctx.body["token"].encode()).hexdigest()
    row = await ctx.repo.one("SELECT * FROM institution_invitations WHERE token_hash=:hash",{"hash":hashed},required=False)
    if not row or row["target_phone_e164"] != ctx.claims["phone_number"]:
        fail(422,"INVALID_INVITATION","The invitation is unavailable for this identity.")
    institution = await ctx.repo.get("institutions",row["institution_id"],lock=getattr(ctx,"lock_mode",False))
    if institution["verification_status"] != "APPROVED":
        fail(422,"INVALID_INVITATION","The invitation is unavailable for this identity.")
    ctx.iid = str(institution["id"])
    if row["status"] == "ACCEPTED":
        await ctx.access("institutions",institution["id"])
    elif row["status"] != "PENDING" or row["expires_at"] <= datetime.now(timezone.utc):
        fail(422,"INVALID_INVITATION","The invitation is unavailable for this identity.")
    await ctx.check_generation()


@endpoint("POST", "/institution-invitations/accept", model="InviteAccept", prepare=prepare_accept)
async def accept_invitation(ctx, body):
    return await service.accept(ctx, body)


@endpoint("GET", "/institutions/{iid}/invitations", prepare=prepare("institutions", "iid", "O"), query=("status",))
async def invitations(ctx, _):
    async def serialize(row):
        return service.invitation_resource(row)
    filters = {"institution_id": ctx.iid}
    if ctx.request.query_params.get("status"):
        filters["status"] = ctx.request.query_params["status"]
    return await ctx.page("institution_invitations", filters, serialize)


async def invitation(ctx):
    row = await ctx.repo.get("institution_invitations", ctx.request.path_params["invite_id"], lock=True)
    if str(row["institution_id"]) != ctx.iid:
        fail(404, "RESOURCE_NOT_FOUND", "Invitation is unavailable.")
    return row


@endpoint("DELETE", "/institutions/{iid}/invitations/{invite_id}", prepare=prepare("institutions", "iid", "O"))
async def revoke_invitation(ctx, _):
    require_flow()
    row = await invitation(ctx)
    ctx.match(row)
    if row["status"] != "PENDING":
        fail(409, "INVITATION_NOT_PENDING", "Only pending invitations can be revoked.")
    row = await ctx.repo.update("institution_invitations", row["id"], {"status": "REVOKED"}, bump=True)
    ctx.revision = row["input_revision"]
    await ctx.repo.event(ctx.actor, "INVITATION_REVOKED", "institution_invitations", row)
    return service.invitation_resource(row)


@endpoint("POST", "/institutions/{iid}/invitations/{invite_id}/reissue", status=201, prepare=prepare("institutions", "iid", "O"))
async def reissue_invitation(ctx, _):
    row = await invitation(ctx)
    return await service.invite(ctx, {"target_phone_e164": row["target_phone_e164"], "permission": row["permission"]}, old=row)
