"""Own inbox and deletion access remains available without tenant membership."""
from datetime import datetime, timezone
from app.institution_access import recent_auth, operator_access
from app.institution_context import endpoint
from app.institution_errors import fail
from services.institution import privacy_service as service
from services.institution.base import public
from services.institution.job_service import enqueue, accepted
from models.institution.common import digest, normalize


def own(ctx):
    if ctx.actor.get("account_domain") != "INSTITUTION":
        fail(403, "PERMISSION_DENIED", "Use the institution account privacy flow.")


@endpoint("GET", "/institution-account/notifications")
async def notifications(ctx, _):
    own(ctx)
    return await ctx.page("institution_notifications", {"recipient_user_id": ctx.actor["id"]}, lambda r: public("NotificationResource", r))


async def notification_access(ctx):
    own(ctx)
    row = await ctx.repo.get("institution_notifications", ctx.request.path_params["notification_id"], lock=getattr(ctx, "lock_mode", False))
    if str(row["recipient_user_id"]) != str(ctx.actor["id"]):
        fail(404, "RESOURCE_NOT_FOUND", "Notification is unavailable.")
    ctx.item = row


@endpoint("PATCH", "/institution-account/notifications/{notification_id}", model="NotificationPatch", prepare=notification_access)
async def mark_read(ctx, body):
    ctx.match(ctx.item)
    row = await ctx.repo.update("institution_notifications", ctx.item["id"], {"read_at": ctx.item["read_at"] or datetime.now(timezone.utc)}, bump=True)
    ctx.revision = row["input_revision"]
    return public("NotificationResource", row)


@endpoint("POST", "/institution-account/deletion-requests", model="DeletionCreate", status=201)
async def request_deletion(ctx, body):
    own(ctx)
    return await service.create(ctx, body)


@endpoint("GET", "/institution-account/deletion-requests")
async def deletion_requests(ctx, _):
    own(ctx)
    return await ctx.page("institution_deletion_requests", {"user_id": ctx.actor["id"]}, service.resource)


async def own_request(ctx):
    own(ctx)
    row = await ctx.repo.get("institution_deletion_requests", ctx.request.path_params["request_id"], lock=getattr(ctx, "lock_mode", False))
    if str(row["user_id"]) != str(ctx.actor["id"]):
        fail(404, "RESOURCE_NOT_FOUND", "Privacy request is unavailable.")
    ctx.item = row


@endpoint("GET", "/institution-account/deletion-requests/{request_id}", prepare=own_request)
async def get_request(ctx, _):
    return service.resource(ctx.item)


@endpoint("POST", "/institution-account/deletion-requests/{request_id}/cancel", prepare=own_request)
async def cancel(ctx, body):
    recent_auth(ctx.claims)
    ctx.match(ctx.item)
    if ctx.item["status"] not in {"REQUESTED", "NEEDS_ACTION", "APPROVED"}:
        fail(409, "PRIVACY_NOT_CANCELLABLE", "This request can no longer be cancelled.")
    row = await ctx.repo.update("institution_deletion_requests", ctx.item["id"], {"status": "CANCELLED"}, bump=True)
    await ctx.repo.event(ctx.actor, "PRIVACY_CANCELLED", "institution_deletion_requests", row)
    ctx.revision = row["input_revision"]
    return service.resource(row)


async def privacy_operator(ctx):
    await operator_access(ctx.repo, ctx.actor, "PRIVACY_OPERATIONS")
    rid = ctx.request.path_params.get("request_id")
    if rid:
        ctx.item = await ctx.repo.get("institution_deletion_requests", rid, lock=getattr(ctx, "lock_mode", False))
        if str(ctx.item["user_id"]) == str(ctx.actor["id"]):
            fail(403, "REVIEWER_CONFLICT", "Privacy execution requires an independent operator.")


@endpoint("GET", "/admin/deletion-requests", prepare=privacy_operator, query=("status",))
async def privacy_queue(ctx, _):
    filters = {"status": ctx.request.query_params["status"]} if ctx.request.query_params.get("status") else {}
    return await ctx.page("institution_deletion_requests", filters, lambda row: admin_resource(ctx,row))


async def admin_resource(ctx,row):
    memberships = await ctx.repo.find("institution_users",{"user_id":row["user_id"]})
    receipts = [{"provider":provider.upper(),"request_reference":str(row["id"]),"status":"SUCCEEDED","completed_at":row["completed_at"]} for provider in row["resolution"].get("provider_confirmations",{}) if provider in {"firebase","storage"}]
    return {**service.resource(row),"requester_id":str(row["user_id"]),"policy_version":str(row["policy_version"]) if row["policy_version"] else None,"plan_hash":row["plan_hash"],"retained_items":row["resolution"].get("retained_items",[]),"owned_institution_ids":list({str(m["institution_id"]) for m in memberships if m["permission"]=="OWNER"}),"provider_receipts":receipts}


@endpoint("GET", "/admin/deletion-requests/{request_id}", prepare=privacy_operator)
async def privacy_detail(ctx, _):
    row = ctx.item
    selected = await service.policy(ctx.repo, row["policy_version"])
    proposed = await service.plan(ctx.repo, row["user_id"], selected)
    return {**await admin_resource(ctx,row), "policy_version": str(selected["id"]), "plan": normalize(proposed), "plan_hash": digest(proposed)}


@endpoint("POST", "/admin/deletion-requests/{request_id}/decision", model="DeletionDecision", prepare=privacy_operator)
async def decision(ctx, body):
    row = ctx.item
    ctx.match(row)
    if row["status"] not in {"REQUESTED", "NEEDS_ACTION"}:
        fail(409, "PRIVACY_STATE_CHANGED", "This privacy request cannot accept this decision.")
    selected = await service.policy(ctx.repo, body["policy_version"])
    proposed = await service.plan(ctx.repo, row["user_id"], selected)
    if digest(proposed) != body["expected_plan_hash"]:
        fail(409, "PRIVACY_PLAN_CHANGED", "Review the current policy and action plan.")
    action = body["action"]
    if action not in {"NEEDS_ACTION", "REJECT", "EXECUTE"}:
        fail(422, "INVALID_DECISION", "Unsupported privacy decision.")
    remaining = await service.blockers(ctx.repo, row["user_id"])
    await service.validate_retained(ctx.repo, row["user_id"], selected, body["retained_items"], proposed)
    if action == "EXECUTE" and (remaining or body["required_actions"] or not row["verified_at"]):
        fail(409, "PRIVACY_OWNERSHIP_UNRESOLVED", "Resolve required verified identity and ownership actions before execution.")
    resolution = {**row["resolution"], "plan": proposed, "rules": normalize(selected), "retained_items": body["retained_items"], "required_actions": remaining + body["required_actions"], "decision_reason": body["reason"]}
    row = await ctx.repo.update("institution_deletion_requests", row["id"], {"status": "APPROVED" if action == "EXECUTE" else "REJECTED" if action == "REJECT" else "NEEDS_ACTION", "policy_version": selected["id"], "plan_hash": digest(proposed), "operator_id": ctx.actor["id"], "resolution": resolution}, bump=True)
    await ctx.repo.event(ctx.actor, "PRIVACY_" + action, "institution_deletion_requests", row, reason=body["reason"])
    ctx.revision = row["input_revision"]
    if action == "EXECUTE":
        ctx.status_code = 202
        job = await enqueue(ctx, "account_deletion", row, {"plan": proposed, "input_hash": row["plan_hash"]}, asset_type="DELETION_REQUEST")
        # Job actor is the independent operator; target remains frozen request.user_id.
        await ctx.repo.update("institution_deletion_requests", row["id"], {"job_id": job["id"]})
        return accepted(job)
    return service.resource(row)
