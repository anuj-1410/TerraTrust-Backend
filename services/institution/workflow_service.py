"""Scoped renewable review claims and atomic independent finalization."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from uuid import uuid4

from sqlalchemy import text

from app.institution_access import independent_reviewer, require_flow
from app.institution_errors import fail
from models.institution.common import normalize
from services.institution.base import public, notification

TABLES = {"REGISTRATION": "institution_registrations", "CAMPUS_CONTROL": "campuses", "BOUNDARY": "campus_boundaries", "MASK": "campus_vegetation_masks", "PLOT_SET": "campus_plot_sets", "SURVEY": "campus_tree_surveys", "TREE_AUDIT": "campus_tree_audits", "INSTITUTION_AUDIT": "institution_audits"}


def case_resource(row, detail=False):
    return public("ReviewCaseResource", row, case_input_revision=row["input_revision"], **({"resource_snapshot": row["resource_snapshot"], "evidence": []} if detail else {}))


async def prepare_case(ctx):
    cid = ctx.request.path_params.get("case_id") or (ctx.body or {}).get("review_case_id")
    if not cid:
        fail(422, "REVIEW_CASE_REQUIRED", "Select the exact review case.")
    case = await ctx.repo.get("review_cases", cid)
    await independent_reviewer(ctx.repo, ctx.actor, case)
    ctx.iid, ctx.case = str(case["institution_id"]), case
    if getattr(ctx, "lock_mode", False):
        await ctx.repo.get("institutions", ctx.iid, lock=True)
        resource = await ctx.repo.get(TABLES[case["resource_type"]], case["resource_id"], lock=True)
        case = await ctx.repo.get("review_cases", cid, lock=True)
        ctx.case, ctx.parent = case, resource
    else:
        ctx.parent = await ctx.repo.get(TABLES[case["resource_type"]], case["resource_id"])
    # Nested route IDs never choose a different resource than the claimed case.
    identifiers = {"iid": "REGISTRATION", "cid": "CAMPUS_CONTROL", "bid": "BOUNDARY", "mid": "MASK", "sid": "SURVEY", "taid": "TREE_AUDIT", "aid": "INSTITUTION_AUDIT"}
    for key, kind in identifiers.items():
        if key in ctx.request.path_params:
            expected = ctx.iid if key == "iid" else str(ctx.parent["campus_id"]) if key == "cid" and case["resource_type"] == "PLOT_SET" else str(case["resource_id"])
            if ctx.request.path_params[key] != expected or (key != "cid" and case["resource_type"] != kind):
                fail(404, "RESOURCE_NOT_FOUND", "Review case is unavailable for this resource.")
    await ctx.check_generation()


async def claim(ctx, body):
    case = ctx.case
    if set(body) - {"current_revision", "action"} or type(body.get("current_revision")) is not int:
        fail(422, "INVALID_CLAIM", "Supply the current case counter and claim action.")
    if body["current_revision"] != case["input_revision"]:
        fail(409, "CASE_REVISION_CONFLICT", "The review claim changed.", current_revision=case["input_revision"])
    if case["status"] == "CLOSED":
        fail(409, "CASE_SUPERSEDED", "This review case is closed.")
    action, now = body.get("action", "CLAIM"), datetime.now(timezone.utc)
    current = str(case["assigned_reviewer"]) == str(ctx.actor["id"])
    live = case["claim_expires_at"] and case["claim_expires_at"] > now
    if action not in {"CLAIM", "RENEW", "RELEASE"} or (action in {"RENEW", "RELEASE"} and not current):
        fail(403, "PERMISSION_DENIED", "Only the current claimant may renew or release this case.")
    if action == "CLAIM" and live and not current:
        fail(409, "CASE_ALREADY_CLAIMED", "Another independent reviewer holds this case.")
    reset = action == "RELEASE" or not current or not live
    values = {"status": "OPEN" if action == "RELEASE" else "CLAIMED", "assigned_reviewer": None if action == "RELEASE" else ctx.actor["id"], "claim_expires_at": None if action == "RELEASE" else now + timedelta(minutes=30)}
    if reset:
        values.update(decision=None, approved_snapshot_id=None)
        if case["resource_type"] == "INSTITUTION_AUDIT":
            await ctx.repo.update("institution_audits", case["resource_id"], {"reviewed_snapshot_id": None})
    saved = await ctx.repo.update("review_cases", case["id"], values, bump=True)
    await ctx.repo.event(ctx.actor, "REVIEW_" + action, "review_cases", saved, details={"prior_decision": case["decision"], "prior_approved_snapshot_id": normalize(case["approved_snapshot_id"])})
    ctx.revision = saved["input_revision"]
    return case_resource(saved, True)


def require_claim(ctx, case_counter):
    case = ctx.case
    if case["reason"] == "INPUT_SUPERSEDED":
        fail(409, "CASE_SUPERSEDED", "The reviewed input has been superseded.")
    if case["status"] != "CLAIMED" or str(case["assigned_reviewer"]) != str(ctx.actor["id"]) or not case["claim_expires_at"] or case["claim_expires_at"] <= datetime.now(timezone.utc):
        fail(409, "REVIEW_CLAIM_REQUIRED", "Claim or renew this exact review case before deciding.")
    if case["input_revision"] != case_counter:
        fail(409, "CASE_REVISION_CONFLICT", "The case counter changed.", current_revision=case["input_revision"])


async def review(ctx, body):
    if body["decision"] in {"SUSPEND", "RESUME"}:
        case = ctx.case
        if case["resource_type"] != "REGISTRATION" or ctx.request.path_params.get("iid") != ctx.iid:
            fail(422, "INVALID_REVIEW_DECISION", "Authority access actions require the approved institution registration case.")
        institution = await ctx.repo.get("institutions", ctx.iid, lock=True)
        ctx.match(institution)
        if case["input_revision"] != body["current_revision"] or case["input_hash"] != body["expected_input_hash"] or not body["reason"].strip():
            fail(409, "INPUT_HASH_CHANGED", "Bind the access action to the exact approved authority evidence and current case counter.")
        action = body["decision"]
        if institution["verification_status"] != ("APPROVED" if action == "SUSPEND" else "SUSPENDED") or ctx.parent["status"] != "APPROVED":
            fail(409, "INVALID_ACCESS_STATE", "This authority access transition is unavailable.")
        saved = await ctx.repo.update("institutions", ctx.iid, {"verification_status":"SUSPENDED" if action == "SUSPEND" else "APPROVED", "reviewed_by":ctx.actor["id"], "reviewed_at":datetime.now(timezone.utc), "review_reason":body["reason"]}, bump=True)
        await ctx.repo.event(ctx.actor, "INSTITUTION_"+action, "institutions", saved, reason=body["reason"])
        ctx.revision = saved["input_revision"]
        from services.institution.institution_service import institution_resource
        return await institution_resource(ctx,saved)
    require_flow()
    row, case = ctx.parent, ctx.case
    ctx.match(row)
    require_claim(ctx, body["current_revision"])
    expected = row.get("frozen_input_hash") or row.get("input_hash") or row.get("control_input_hash")
    if body["expected_input_hash"] != expected or case["input_hash"] != expected:
        fail(409, "INPUT_HASH_CHANGED", "Review must bind to the exact frozen input.")
    decision, kind = body["decision"], case["resource_type"]
    allowed = {"REGISTRATION": {"START", "APPROVE", "RETURN", "REJECT"}, "CAMPUS_CONTROL": {"START", "APPROVE", "RETURN", "REJECT"}, "BOUNDARY": {"START", "APPROVE", "RETURN"}, "MASK": {"START", "APPROVE", "RETURN"}, "PLOT_SET": {"START", "APPROVE", "RETURN"}, "SURVEY": {"START", "ACCEPT", "RETURN"}, "TREE_AUDIT": {"START", "ACCEPT", "RETURN"}, "INSTITUTION_AUDIT": {"START", "APPROVE", "RETURN"}}
    if decision not in allowed[kind] or not body["reason"].strip():
        fail(422, "INVALID_REVIEW_DECISION", "Decision or documented reason is invalid for this resource.")
    if kind in {"REGISTRATION", "SURVEY", "INSTITUTION_AUDIT"}:
        status_key = "business_status" if kind == "INSTITUTION_AUDIT" else "status"
        if row[status_key] not in {"SUBMITTED", "UNDER_REVIEW"}:
            fail(409, "INVALID_REVIEW_STATE", "Resource is not awaiting independent review.")
        if decision != "START" and row[status_key] != "UNDER_REVIEW":
            fail(409, "REVIEW_NOT_STARTED", "Start inspection before making a decision.")
    now = datetime.now(timezone.utc)
    values = {"reviewed_by": ctx.actor["id"], "reviewed_at": now, "review_reason": body["reason"]}
    table = TABLES[kind]
    case_values = {"decision": decision, "reason": body["reason"], "field_issues": body["field_issues"]}
    positive = decision in {"APPROVE", "ACCEPT"}
    if positive:
        # The case's source set is frozen. New approvals cannot accept purged,
        # quarantined, or incomplete files merely because their IDs still exist.
        for evidence_id in case["evidence_ids"]:
            source = await ctx.repo.get("institution_audit_evidence", evidence_id)
            if source["state"] != "READY" or source.get("bytes_deleted_at") or not source.get("byte_hash"):
                fail(422, "EVIDENCE_NOT_READY", "Every frozen review source must remain verified and available at approval.")
    if kind == "REGISTRATION":
        values["status"] = "UNDER_REVIEW" if decision == "START" else "APPROVED" if positive else "NEEDS_CORRECTION" if decision == "RETURN" else "REJECTED"
        if positive:
            await ctx.repo.update("institutions", ctx.iid, {"verification_status": "APPROVED", "profile": row["profile_snapshot"], "reviewed_by": ctx.actor["id"], "reviewed_at": now, "review_reason": body["reason"]})
            await ctx.repo.conn.execute(text("UPDATE institution_users SET status='ACTIVE',joined_at=now(),updated_at=now() WHERE institution_id=:iid AND user_id=:uid AND status='PENDING'"), {"iid": ctx.iid, "uid": row["created_by"]})
            # joined_at durably records onboarding completion. Bootstrap copies
            # it to the user's latch under that identity's lock; updating another
            # identity here would invert identity -> tenant lock ordering.
        elif decision == "REJECT":
            await ctx.repo.update("institutions", ctx.iid, {"verification_status": "REJECTED", "review_reason": body["reason"]})
    elif kind == "CAMPUS_CONTROL":
        values = {"control_status": "APPROVED" if positive else "REJECTED" if decision == "REJECT" else "PENDING"}
        if positive:
            if not row["control_evidence_ids"]:
                fail(422, "EVIDENCE_REQUIRED", "Campus control needs verified authority evidence.")
            values["control_approved_hash"] = row["control_input_hash"]
    elif kind == "BOUNDARY":
        if positive:
            campus = await ctx.repo.get("campuses", row["campus_id"], lock=True)
            from services.institution.campus_service import controlled
            controlled(campus)
            await ctx.repo.update("campuses", campus["id"], {"current_boundary_id": row["id"], "current_mask_id": None, "current_plot_set_id": None})
        values["review_status"] = "APPROVED" if positive else "NEEDS_CORRECTION" if decision == "RETURN" else row["review_status"]
    elif kind in {"MASK", "PLOT_SET"}:
        if positive:
            campus = await ctx.repo.get("campuses", row["campus_id"], lock=True)
            if str(campus["current_boundary_id"]) != str(row["boundary_id"]):
                fail(409, "INPUT_HASH_CHANGED", "Campus boundary changed before approval.")
            if kind == "MASK" and (row["processing_status"] != "SUCCEEDED" or not row["eligible_geometry"]):
                fail(422, "RESULT_NOT_READY", "A successful eligible-area candidate is required.")
            if kind == "PLOT_SET":
                job = await ctx.repo.get("processing_jobs", row["generation_job_id"])
                if job["status"] != "SUCCEEDED" or str(campus["current_mask_id"]) != str(row["mask_id"]):
                    fail(422, "RESULT_NOT_READY", "A successful compatible plot candidate is required.")
                await ctx.repo.conn.execute(text("UPDATE campus_sampling_plots SET review_status='APPROVED' WHERE plot_set_id=:id"), {"id": row["id"]})
            await ctx.repo.update("campuses", campus["id"], {"current_mask_id" if kind == "MASK" else "current_plot_set_id": row["id"], **({"current_plot_set_id": None} if kind == "MASK" else {})})
        values["review_status"] = "APPROVED" if positive else "NEEDS_CORRECTION" if decision == "RETURN" else row["review_status"]
    elif kind == "SURVEY":
        values["status"] = "UNDER_REVIEW" if decision == "START" else "ACCEPTED" if positive else "NEEDS_CORRECTION"
    elif kind == "TREE_AUDIT":
        if positive and (row["processing_status"] != "SUCCEEDED" or not row["result_hash"] or body["expected_result_hash"] != row["result_hash"]):
            fail(409, "RESULT_HASH_CHANGED", "Accept only the exact successful stock result.")
        values["review_status"] = "ACCEPTED" if positive else "NEEDS_CORRECTION" if decision == "RETURN" else row["review_status"]
    elif kind == "INSTITUTION_AUDIT":
        values.pop("review_reason")
        values["latest_review_reason"] = body["reason"]
        values["business_status"] = "UNDER_REVIEW" if decision in {"START", "APPROVE"} else "NEEDS_CORRECTION"
        if positive:
            if not row["preview_snapshot_id"] or row["processing_status"] != "SUCCEEDED":
                fail(409, "RESULT_NOT_READY", "Wait for the successful frozen calculation.")
            snapshot = await ctx.repo.get("institution_calculation_snapshots", row["preview_snapshot_id"])
            if snapshot["input_hash"] != row["frozen_input_hash"] or snapshot["result_hash"] != body["expected_result_hash"] or snapshot["result"]["actual_total"] is None:
                fail(409, "RESULT_HASH_CHANGED", "Approve the exact complete frozen calculation.")
            values["reviewed_snapshot_id"] = snapshot["id"]
            case_values.update(approved_snapshot_id=snapshot["id"], result_hash=snapshot["result_hash"])
        elif decision == "RETURN":
            await ctx.repo.update("institution_audit_periods", row["logical_audit_id"], {"active_draft_audit_id": None})
    if decision not in {"START"} and not (kind == "INSTITUTION_AUDIT" and positive):
        case_values["status"] = "CLOSED"
    saved = await ctx.repo.update(table, row["id"], values)
    saved_case = await ctx.repo.update("review_cases", case["id"], case_values, bump=True)
    await ctx.repo.event(ctx.actor, "REVIEW_" + decision, table, saved, reason=body["reason"], details={"case_id": str(case["id"]), "resource_input_revision": row["input_revision"], "case_input_revision": saved_case["input_revision"], "result_hash": body["expected_result_hash"]})
    await notification(ctx.repo, row.get("created_by") or row.get("submitted_by"), row, "REVIEW_DECIDED", "INSTITUTION_AUDIT" if kind == "INSTITUTION_AUDIT" else "REGISTRATION" if kind == "REGISTRATION" else "CAMPUS", "Independent review updated your resource.")
    ctx.revision = row["input_revision"]
    return await review_resource(ctx, kind, saved)


async def review_resource(ctx, kind, row):
    if kind == "REGISTRATION":
        from services.institution.institution_service import registration_resource
        return await registration_resource(ctx, row)
    if kind == "CAMPUS_CONTROL":
        from services.institution.campus_service import resource
        return await resource(ctx, row)
    if kind == "SURVEY":
        from services.institution.survey_service import survey_resource
        return await survey_resource(ctx, row)
    if kind == "TREE_AUDIT":
        from services.institution.survey_service import tree_audit_resource
        return tree_audit_resource(row)
    if kind == "INSTITUTION_AUDIT":
        from services.institution.annual_service import resource
        return await resource(ctx, row)
    return public({"BOUNDARY": "BoundaryResource", "MASK": "MaskResource", "PLOT_SET": "PlotSetResource"}[kind], row, overlap_flags=[])


async def finalize(ctx, body):
    require_flow()
    case, audit = ctx.case, ctx.parent
    ctx.match(audit)
    require_claim(ctx, body["case_input_revision"])
    if case["resource_type"] != "INSTITUTION_AUDIT" or case["decision"] != "APPROVE" or audit["business_status"] != "UNDER_REVIEW":
        fail(409, "APPROVED_REVIEW_REQUIRED", "An independently approved current claim is required.")
    snapshot = await ctx.repo.get("institution_calculation_snapshots", body["snapshot_id"])
    if str(snapshot["id"]) != str(case["approved_snapshot_id"]) or str(snapshot["audit_id"]) != str(audit["id"]) or snapshot["input_hash"] != body["expected_input_hash"] or snapshot["input_hash"] != audit["frozen_input_hash"] or snapshot["result_hash"] != body["expected_result_hash"] or snapshot["result_hash"] != case["result_hash"]:
        fail(409, "RESULT_HASH_CHANGED", "Finalization must bind to the exact independently reviewed hashes.")
    if datetime.now(ZoneInfo(audit["timezone"])).date() <= audit["period_end"]:
        fail(422, "PERIOD_NOT_ENDED", "Finalization cannot precede the end of the reporting period.")
    family = await ctx.repo.get("institution_audit_periods", audit["logical_audit_id"], lock=True)
    if family["current_final_audit_id"]:
        await ctx.repo.update("institution_audits", family["current_final_audit_id"], {"business_status": "SUPERSEDED"})
    result_id = str(uuid4())
    output = {**snapshot["result"], "id": result_id}
    result = await ctx.repo.insert("institution_audit_results", {"id": result_id, "institution_id": ctx.iid, "audit_id": audit["id"], "snapshot_id": snapshot["id"], "result_revision": audit["revision"], "input_hash": snapshot["input_hash"], "result_hash": snapshot["result_hash"], "result": output})
    saved = await ctx.repo.update("institution_audits", audit["id"], {"business_status": "FINALIZED", "current_result_id": result["id"], "finalized_by": ctx.actor["id"], "finalized_at": datetime.now(timezone.utc)})
    await ctx.repo.update("institution_audit_periods", family["id"], {"current_final_audit_id": audit["id"], "active_draft_audit_id": None})
    await ctx.repo.update("review_cases", case["id"], {"status": "CLOSED"}, bump=True)
    await ctx.repo.event(ctx.actor, "AUDIT_FINALIZED", "institution_audits", saved, details={"snapshot_id": str(snapshot["id"]), "result_hash": snapshot["result_hash"]})
    ctx.revision = saved["input_revision"]
    from services.institution.annual_service import resource
    return await resource(ctx, saved)
