"""Controlled registrations, identity-targeted invitations and owner protection."""
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from uuid import uuid4

from sqlalchemy import text

from app.config import settings
from app.institution_access import require_flow, recent_auth
from app.institution_errors import fail
from models.institution.common import digest, normalize
from services.institution.base import public, draft, evidence, link_evidence, case_for


async def institution_resource(ctx, row):
    members = await ctx.repo.find("institution_users", {"institution_id": row["id"], "user_id": ctx.actor["id"]})
    member = members[0] if members else {}
    if member.get("status") == "ACTIVE" and (member.get("permission") == "VIEWER" or not settings.ENABLE_INSTITUTION_FLOW):
        # Tenant discovery must obey the same frozen projection as detail reads.
        audits = await ctx.repo.find("institution_audits", {"institution_id": row["id"], "business_status": "FINALIZED"})
        audits = sorted(audits, key=lambda audit: audit["period_end"], reverse=True)
        if not audits:
            # A membership may predate the first release. Expose only navigation
            # identity and permission, with no mutable scientific/profile fields.
            return {"id": str(row["id"]), "permission": "VIEWER", "membership_status": member["status"], "released_snapshot_id": None}
        snapshot = await ctx.repo.get("institution_calculation_snapshots", audits[0]["reviewed_snapshot_id"])
        frozen = snapshot["frozen_input"]["audit"]["boundary_snapshot"]["institution"]
        return public("InstitutionResource", frozen, permission="VIEWER", membership_status=member["status"], registration_input_revision=None)
    reg = await ctx.repo.get("institution_registrations", row["current_registration_id"]) if row.get("current_registration_id") else None
    return public("InstitutionResource", row, permission=member.get("permission"), membership_status=member.get("status"), registration_input_revision=reg["input_revision"] if reg else None)


async def registration_resource(ctx, row):
    cases = await ctx.repo.find("review_cases", {"resource_id": row["id"], "resource_type": "REGISTRATION"})
    return public("RegistrationResource", row, review_case_id=cases[0]["id"] if cases else None, copied_id_mappings=[])


async def create(ctx, body):
    require_flow()
    if body["registration_draft_owner_id"] != str(ctx.actor["id"]):
        fail(403, "PERMISSION_DENIED", "Registration belongs to the authenticated identity.")
    await ctx.repo.conn.execute(text("SELECT id FROM users WHERE id=:id FOR UPDATE"), {"id": ctx.actor["id"]})
    previous = await ctx.repo.find("institutions", {"created_by": ctx.actor["id"]})
    existing_memberships = await ctx.repo.find("institution_users", {"user_id": ctx.actor["id"]})
    grant = None
    if previous or existing_memberships:
        grant_id = body.get("operator_authorization_id")
        if not grant_id:
            fail(409, "REGISTRATION_AUTHORIZATION_INVALID", "Resume your registration or obtain an operator authorization.")
        grant = await ctx.repo.get("institution_registration_authorizations", grant_id, lock=True)
        owners = await ctx.repo.rows("SELECT iu.id FROM institution_users iu JOIN institutions i ON i.id=iu.institution_id WHERE iu.user_id=:uid AND iu.status='ACTIVE' AND iu.permission='OWNER' AND i.verification_status='APPROVED'", {"uid": ctx.actor["id"]})
        if not owners or str(grant["target_user_id"]) != str(ctx.actor["id"]) or grant["consumed_at"] or grant["revoked_at"] or grant["expires_at"] <= datetime.now(timezone.utc):
            fail(409, "REGISTRATION_AUTHORIZATION_INVALID", "Registration authorization is unavailable.")
    manifests = await evidence(ctx.repo, body["evidence_ids"], "REGISTRATION_DRAFT", ctx.actor["id"], None, ctx.actor)
    institution = await ctx.repo.insert("institutions", {"profile": body["profile"], "consolidation_approach": "OPERATIONAL_CONTROL", "verification_status": "PENDING", "created_by": ctx.actor["id"]})
    iid = institution["id"]
    reg = await ctx.repo.insert("institution_registrations", {"institution_id": iid, "logical_registration_id": str(uuid4()), "revision": 1, "created_by": ctx.actor["id"], "status": "DRAFT", "profile_snapshot": body["profile"], "evidence_ids": body["evidence_ids"], "input_hash": digest({"profile": body["profile"], "evidence": manifests})})
    await ctx.repo.insert("institution_users", {"institution_id": iid, "user_id": ctx.actor["id"], "permission": "OWNER", "status": "PENDING", "added_by": ctx.actor["id"]})
    institution = await ctx.repo.update("institutions", iid, {"current_registration_id": reg["id"]})
    await link_evidence(ctx.repo, body["evidence_ids"], "REGISTRATION", reg["id"], iid, ctx.actor)
    if grant:
        await ctx.repo.update("institution_registration_authorizations", grant["id"], {"consumed_at": datetime.now(timezone.utc), "institution_id": iid})
    await ctx.repo.event(ctx.actor, "REGISTRATION_CREATED", "institutions", institution)
    ctx.revision = 1
    result = await institution_resource(ctx, institution)
    result["registration_id"] = str(reg["id"])
    return result


async def patch(ctx, body):
    require_flow()
    institution = await ctx.repo.get("institutions", ctx.iid, lock=True)
    pending = institution["verification_status"] in {"PENDING", "REJECTED"}
    target = await ctx.repo.get("institution_registrations", institution["current_registration_id"], lock=True) if pending else institution
    ctx.match(target)
    if pending:
        draft(target)
    elif ctx.member["permission"] != "OWNER":
        fail(403, "PERMISSION_DENIED", "Only the owner may edit this profile.")
    elif "evidence_ids" in body:
        fail(422, "INVALID_PAYLOAD", "Approved authority evidence requires an independent correction review.")
    key = "profile_snapshot" if pending else "profile"
    profile = {**target[key]}
    for field, value in body.get("profile", {}).items():
        profile[field] = {**profile[field], **value} if isinstance(value, dict) and isinstance(profile.get(field), dict) else value
    from models.institution.contracts import parse
    parse("ProfileData", profile)
    values = {key: profile}
    if pending:
        ids = body.get("evidence_ids", list(map(str, target["evidence_ids"])))
        manifests = await evidence(ctx.repo, ids, "REGISTRATION", target["id"], ctx.iid)
        values.update(evidence_ids=ids, input_hash=digest({"profile": profile, "evidence": manifests}))
    saved = await ctx.repo.update("institution_registrations" if pending else "institutions", target["id"], values, bump=True)
    ctx.revision = saved["input_revision"]
    await ctx.repo.event(ctx.actor, "PROFILE_SAVED", "institution_registrations" if pending else "institutions", saved)
    return await institution_resource(ctx, await ctx.repo.get("institutions", ctx.iid))


async def submit_registration(ctx, body):
    require_flow()
    institution = await ctx.repo.get("institutions", ctx.iid, lock=True)
    reg = await ctx.repo.get("institution_registrations", institution["current_registration_id"], lock=True)
    ctx.match(reg)
    draft(reg)
    if body["expected_input_hash"] != reg["input_hash"] or set(body["evidence_ids"]) != set(map(str, reg["evidence_ids"])):
        fail(409, "INPUT_HASH_CHANGED", "Save the selected profile and evidence before submission.")
    await evidence(ctx.repo, body["evidence_ids"], "REGISTRATION", reg["id"], ctx.iid, required=True)
    reg = await ctx.repo.update("institution_registrations", reg["id"], {"status": "SUBMITTED", "frozen_input_hash": reg["input_hash"], "submitted_by": ctx.actor["id"], "submitted_at": datetime.now(timezone.utc)})
    await case_for(ctx.repo, ctx.actor, "institution_registrations", reg, "REGISTRATION", await registration_resource(ctx, reg))
    await ctx.repo.conn.execute(text("UPDATE users SET institution_onboarding_status='PENDING_REVIEW' WHERE id=:id AND institution_onboarding_completed_at IS NULL"), {"id": ctx.actor["id"]})
    ctx.revision = reg["input_revision"]
    return await registration_resource(ctx, reg)


async def registration_revision(ctx, body):
    require_flow()
    institution = await ctx.repo.get("institutions", ctx.iid, lock=True)
    old = await ctx.repo.get("institution_registrations", institution["current_registration_id"], lock=True)
    ctx.match(old)
    if body["source_input_revision"] != old["input_revision"] or old["status"] not in {"NEEDS_CORRECTION", "REJECTED"}:
        fail(409, "REVIEW_IN_PROGRESS", "Registration is not available for correction.")
    reg = await ctx.repo.insert("institution_registrations", {"institution_id": ctx.iid, "logical_registration_id": old["logical_registration_id"], "revision": old["revision"] + 1, "created_by": ctx.actor["id"], "status": "DRAFT", "profile_snapshot": old["profile_snapshot"], "evidence_ids": old["evidence_ids"], "input_hash": old["input_hash"], "supersedes_id": old["id"]})
    await link_evidence(ctx.repo, old["evidence_ids"], "REGISTRATION", reg["id"], ctx.iid, ctx.actor)
    await ctx.repo.update("institutions", ctx.iid, {"current_registration_id": reg["id"], "verification_status": "PENDING"})
    await ctx.repo.event(ctx.actor, "REGISTRATION_CORRECTION", "institution_registrations", reg, reason=body["reason"])
    ctx.revision = 1
    return await registration_resource(ctx, reg)


def invitation_resource(row):
    phone = row["target_phone_e164"]
    return public("InvitationResource", row, target_phone_masked=phone[:3] + "*" * max(0, len(phone) - 7) + phone[-4:])


async def invite(ctx, body, old=None):
    require_flow()
    from services.shared.receipt_crypto_service import encrypt_receipt
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=7)
    # Encrypt the receipt locally before making any invitation changes.
    ctx.secret = await encrypt_receipt(token, ctx.receipt_scope, ctx.request_hash, min(expires, datetime.now(timezone.utc) + timedelta(hours=72)))
    if old:
        ctx.match(old)
        if old["status"] != "PENDING":
            fail(409, "INVITATION_NOT_PENDING", "Only a pending invitation may be reissued.")
        await ctx.repo.update("institution_invitations", old["id"], {"status": "REVOKED"}, bump=True)
    else:
        await ctx.repo.conn.execute(text("UPDATE institution_invitations SET status='EXPIRED' WHERE institution_id=:iid AND target_phone_e164=:phone AND status='PENDING' AND expires_at<=now()"), {"iid": ctx.iid, "phone": body["target_phone_e164"]})
    row = await ctx.repo.insert("institution_invitations", {"institution_id": ctx.iid, "target_phone_e164": body["target_phone_e164"], "permission": body["permission"], "created_by": ctx.actor["id"], "token_hash": hashlib.sha256(token.encode()).hexdigest(), "status": "PENDING", "expires_at": expires, "supersedes_id": old["id"] if old else None})
    await ctx.repo.event(ctx.actor, "INVITATION_ISSUED", "institution_invitations", row)
    result = invitation_resource(row)
    # Raw token is added to the HTTP response after the durable receipt is saved.
    ctx.response_secret = token
    return {**result, "invitation_url": None}


async def accept(ctx, body):
    require_flow()
    hashed = hashlib.sha256(body["token"].encode()).hexdigest()
    row = await ctx.repo.one("SELECT * FROM institution_invitations WHERE token_hash=:hash", {"hash": hashed}, required=False)
    if not row or row["target_phone_e164"] != ctx.claims["phone_number"]:
        fail(422, "INVALID_INVITATION", "The invitation is unavailable for this identity.")
    institution = await ctx.repo.get("institutions", row["institution_id"], lock=True)
    row = await ctx.repo.get("institution_invitations",row["id"],lock=True)
    if institution["verification_status"] != "APPROVED":
        fail(422, "INVALID_INVITATION", "The invitation is unavailable for this identity.")
    if row["status"] == "ACCEPTED" and str(row["accepted_by"]) == str(ctx.actor["id"]):
        member = (await ctx.repo.find("institution_users", {"institution_id": row["institution_id"], "user_id": ctx.actor["id"]}))[0]
        return public("MembershipResource", member, display_name=ctx.actor.get("full_name"))
    if row["status"] != "PENDING" or row["expires_at"] <= datetime.now(timezone.utc):
        fail(422, "INVALID_INVITATION", "The invitation is unavailable for this identity.")
    existing = await ctx.repo.find("institution_users", {"institution_id": row["institution_id"], "user_id": ctx.actor["id"]}, lock=True)
    if existing and existing[0]["status"] == "ACTIVE":
        fail(409, "ALREADY_MEMBER", "An existing member's permission must be changed by an owner.")
    values = {"permission": row["permission"], "status": "ACTIVE", "joined_at": datetime.now(timezone.utc), "revoked_at": None}
    member = await ctx.repo.update("institution_users", existing[0]["id"], values, bump=True) if existing else await ctx.repo.insert("institution_users", {**values, "institution_id": row["institution_id"], "user_id": ctx.actor["id"], "added_by": row["created_by"]})
    await ctx.repo.update("institution_invitations", row["id"], {"status": "ACCEPTED", "accepted_by": ctx.actor["id"], "accepted_at": datetime.now(timezone.utc)}, bump=True)
    await ctx.repo.conn.execute(text("UPDATE users SET institution_onboarding_status='COMPLETE',institution_onboarding_completed_at=COALESCE(institution_onboarding_completed_at,now()) WHERE id=:id"), {"id": ctx.actor["id"]})
    await ctx.repo.event(ctx.actor, "MEMBERSHIP_ACCEPTED", "institution_users", member, details={"target_user_id": str(ctx.actor["id"]), "previous_status": existing[0]["status"] if existing else None, "next_status": "ACTIVE", "next_permission": row["permission"], "effective_at": normalize(datetime.now(timezone.utc))})
    ctx.revision = member["input_revision"]
    return public("MembershipResource", member, display_name=ctx.actor.get("full_name"))


async def member_patch(ctx, body):
    require_flow()
    recent_auth(ctx.claims)
    await ctx.repo.get("institutions", ctx.iid, lock=True)
    member = await ctx.repo.one("SELECT * FROM institution_users WHERE institution_id=:iid AND user_id=:uid FOR UPDATE", {"iid": ctx.iid, "uid": ctx.request.path_params["uid"]})
    ctx.match(member)
    if body.get("status") not in (None, "REVOKED"):
        fail(422, "INVALID_PAYLOAD", "Only revocation is accepted through membership status.")
    permission, status = body.get("permission", member["permission"]), body.get("status", member["status"])
    if member["status"] == "ACTIVE" and member["permission"] == "OWNER" and (permission != "OWNER" or status != "ACTIVE"):
        owners = await ctx.repo.find("institution_users", {"institution_id": ctx.iid, "permission": "OWNER", "status": "ACTIVE"})
        if len(owners) <= 1:
            fail(409, "LAST_OWNER_REQUIRED", "Transfer ownership to another active member first.")
    values = {"permission": permission, "status": status}
    if status == "REVOKED":
        values["revoked_at"] = datetime.now(timezone.utc)
    saved = await ctx.repo.update("institution_users", member["id"], values, bump=True)
    await ctx.repo.event(ctx.actor, "MEMBERSHIP_CHANGED", "institution_users", saved, details={"target_user_id": str(member["user_id"]), "previous_permission": member["permission"], "previous_status": member["status"], "next_permission": permission, "next_status": status, "effective_at": normalize(datetime.now(timezone.utc))})
    ctx.revision = saved["input_revision"]
    return public("MembershipResource", saved, display_name=None)
