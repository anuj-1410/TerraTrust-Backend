"""Released reads always project frozen historical fields, never mutable trees."""
from app.institution_errors import fail
from app.institution_access import RELEASED
from services.institution.base import public


def released_public(name, row, **overrides):
    projected = public(name, row, **overrides)
    # IDs of private evidence are not part of a released scientific projection.
    for key in ("evidence_ids", "control_evidence_ids"):
        if key in projected:
            projected[key] = []
    for key in ("control_review_case_id", "review_case_id"):
        if key in projected:
            projected[key] = None
    return projected


async def released_snapshot(ctx, snapshot_id=None):
    sid = snapshot_id or ctx.request.query_params.get("snapshot_id")
    if not sid:
        fail(422, "SNAPSHOT_REQUIRED", "Select an authorized released snapshot.")
    snapshot = await ctx.repo.get("institution_calculation_snapshots", sid)
    audit = await ctx.repo.get("institution_audits", snapshot["audit_id"])
    if str(snapshot["institution_id"]) != ctx.iid or audit["business_status"] not in RELEASED or str(audit["reviewed_snapshot_id"]) != str(sid):
        fail(404, "RESOURCE_NOT_FOUND", "Released snapshot is unavailable.")
    ctx.context_id = str(sid)
    return snapshot


async def released_resource(ctx, kind, resource_id):
    snapshot = await released_snapshot(ctx)
    frozen = snapshot["frozen_input"]
    if kind == "institution":
        row = frozen["audit"]["boundary_snapshot"]["institution"]
        return public("InstitutionResource", row, permission=ctx.member["permission"], membership_status=ctx.member["status"], registration_input_revision=None)
    if kind == "campus":
        for campus in released_campuses(frozen):
            if str(campus["id"]) == str(resource_id):
                return campus
    for link in frozen["tree_links"]:
        stored = link["attribution_snapshot"]
        if kind == "campus" and str(stored["campus"]["id"]) == str(resource_id):
            return released_public("CampusResource", stored["campus"], eligible_area_ha=stored["mask"]["eligible_area_ha"], pending_review_ids=[], open_survey_ids=[], latest_accepted_tree_audit_id=str(stored["assessment"]["id"]))
        if kind in {"boundary", "mask"} and str(stored[kind]["id"]) == str(resource_id):
            return released_public("BoundaryResource" if kind == "boundary" else "MaskResource", stored[kind], overlap_flags=[])
        if kind == "tree":
            for tree in stored["survey"].get("trees", []):
                if str(tree["id"]) == str(resource_id):
                    return public("TreeResource", tree)
        if kind == "tree_audit" and str(stored["assessment"]["id"]) == str(resource_id):
            from services.institution.survey_service import tree_audit_resource
            return tree_audit_resource(stored["assessment"])
    fail(404, "RESOURCE_NOT_FOUND", "Resource is absent from this released snapshot.")


def released_campuses(frozen):
    """Include inventory campuses without inventing a tree assessment for them."""
    campuses = {str(row["id"]): released_public("CampusResource", row, eligible_area_ha=None, pending_review_ids=[], open_survey_ids=[], latest_accepted_tree_audit_id=None) for row in frozen["audit"]["boundary_snapshot"].get("campuses", [])}
    for link in frozen["tree_links"]:
        stored = link["attribution_snapshot"]
        row = stored["campus"]
        campuses[str(row["id"])] = released_public("CampusResource", row, eligible_area_ha=stored["mask"]["eligible_area_ha"], pending_review_ids=[], open_survey_ids=[], latest_accepted_tree_audit_id=str(stored["assessment"]["id"]))
    return list(campuses.values())
