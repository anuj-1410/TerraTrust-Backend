"""Ancillary bytes/status use the same current independent case claim."""
from app.institution_access import independent_reviewer
from app.institution_errors import fail


async def claimed_resource(ctx, iid, resource_id):
    cases = await ctx.repo.rows("SELECT * FROM review_cases WHERE institution_id=:iid AND resource_id=:rid AND status='CLAIMED' AND assigned_reviewer=:uid AND claim_expires_at>now()",{"iid":iid,"rid":resource_id,"uid":ctx.actor["id"]})
    for case in cases:
        await independent_reviewer(ctx.repo,ctx.actor,case)
        ctx.iid,ctx.case = str(iid),case
        return case
    fail(404,"RESOURCE_NOT_FOUND","This resource is unavailable outside a current authorized review claim.")
