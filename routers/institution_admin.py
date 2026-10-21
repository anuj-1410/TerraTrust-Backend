"""Internal operator API: scoped queue, inspect, claim, review and finalize."""
from datetime import datetime, timedelta, timezone

from app.institution_context import endpoint
from app.institution_access import independent_reviewer, operator_access
from app.institution_errors import fail, InstitutionError
from models.institution.common import normalize
from services.institution import workflow_service as service


async def digitizer_access(ctx):
    campus = await ctx.repo.get("campuses", ctx.request.path_params["cid"])
    ctx.iid = str(campus["institution_id"])
    await operator_access(ctx.repo, ctx.actor, "BOUNDARY_REVIEW", iid=ctx.iid)
    if getattr(ctx, "lock_mode", False):
        await ctx.repo.get("institutions", ctx.iid, lock=True)
        campus = await ctx.repo.get("campuses", campus["id"], lock=True)
    ctx.parent, ctx.operator_digitization = campus, True


@endpoint("POST", "/admin/campuses/{cid}/boundaries", model="BoundaryCreate", status=201, prepare=digitizer_access)
async def digitize_boundary(ctx, body):
    if body["source"] != "OPERATOR_DIGITIZED":
        fail(422, "INVALID_BOUNDARY_SOURCE", "Scoped operator digitization must declare its actual source.")
    from services.institution.campus_service import boundary
    return await boundary(ctx, body)


@endpoint("GET", "/admin/review-cases", query=("resource_type", "status"))
async def review_queue(ctx, _):
    rows = await ctx.repo.rows("SELECT c.* FROM review_cases c JOIN operator_grants g ON g.user_id=:uid AND g.revoked_at IS NULL AND g.valid_from<=now() AND g.valid_until>now() AND (g.institution_id=c.institution_id OR g.case_id=c.id) ORDER BY c.created_at DESC,c.id DESC", {"uid": ctx.actor["id"]})
    result, seen = [], set()
    for row in rows:
        if str(row["id"]) in seen:
            continue
        if ctx.request.query_params.get("resource_type") and row["resource_type"] != ctx.request.query_params["resource_type"]:
            continue
        if ctx.request.query_params.get("status") and row["status"] != ctx.request.query_params["status"]:
            continue
        try:
            await independent_reviewer(ctx.repo, ctx.actor, row)
        except InstitutionError:
            continue
        seen.add(str(row["id"]))
        result.append(service.case_resource(row))
    return await ctx.page_rows(result)


@endpoint("GET", "/admin/review-cases/{case_id}", prepare=service.prepare_case)
async def review_detail(ctx, _):
    return service.case_resource(ctx.case, True)


@endpoint("POST", "/admin/review-cases/{case_id}/claim", model="ReviewClaimRequest", prepare=service.prepare_case)
async def claim_review(ctx, body):
    return await service.claim(ctx, body or {})


def register_review(path):
    @endpoint("POST", path, model="ReviewRequest", prepare=service.prepare_case)
    async def review(ctx, body):
        return await service.review(ctx, body)


for path in ("/admin/institutions/{iid}/verification", "/admin/campuses/{cid}/control-review", "/admin/campus-boundaries/{bid}/review", "/admin/campus-masks/{mid}/review", "/admin/campuses/{cid}/sampling-plots/review", "/admin/tree-surveys/{sid}/review", "/admin/campus-tree-audits/{taid}/review", "/institution-audits/{aid}/review"):
    register_review(path)


@endpoint("POST", "/institution-audits/{aid}/finalize", model="FinalizeRequest", prepare=service.prepare_case)
async def finalize_audit(ctx, body):
    return await service.finalize(ctx, body)


async def registration_operator(ctx):
    await operator_access(ctx.repo, ctx.actor, "REGISTRATION_OPERATIONS")


@endpoint("POST", "/admin/institution-registration-authorizations", model="RegistrationAuthorizationRequest", status=201, prepare=registration_operator)
async def registration_authorization(ctx, body):
    if not body or set(body) != {"target_user_id", "reason"} or not isinstance(body["reason"], str) or not 1 <= len(body["reason"]) <= 2000:
        fail(422, "INVALID_PAYLOAD", "Supply the verified target user and a change-record reason.")
    owner = await ctx.repo.one("SELECT iu.user_id FROM institution_users iu JOIN users u ON u.id=iu.user_id JOIN institutions i ON i.id=iu.institution_id WHERE iu.user_id=:uid AND u.account_domain='INSTITUTION' AND iu.permission='OWNER' AND iu.status='ACTIVE' AND i.verification_status='APPROVED' LIMIT 1", {"uid": body["target_user_id"]}, required=False)
    if not owner:
        fail(422, "APPROVED_OWNER_REQUIRED", "The target must be an existing approved institution owner.")
    row = await ctx.repo.insert("institution_registration_authorizations", {"target_user_id": body["target_user_id"], "granted_by": ctx.actor["id"], "expires_at": datetime.now(timezone.utc) + timedelta(days=7), "reason": body["reason"]})
    await ctx.repo.event(ctx.actor, "ADDITIONAL_REGISTRATION_AUTHORIZED", "institution_registration_authorizations", row, reason=body["reason"])
    return normalize({key: row[key] for key in ("id", "target_user_id", "expires_at", "consumed_at")})
