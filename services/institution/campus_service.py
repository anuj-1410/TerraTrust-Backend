"""Campus control, versioned geometry and staged assessment configuration."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.config import settings
from app.institution_access import require_flow
from app.institution_errors import fail
from models.institution.common import digest, normalize
from services.institution.base import public, evidence, case_for
from services.institution.factor_service import approved
from services.institution.job_service import enqueue, accepted
from services.shared.sampling_service import measure


async def resource(ctx, campus):
    mask = await ctx.repo.get("campus_vegetation_masks", campus["current_mask_id"]) if campus["current_mask_id"] else None
    surveys = await ctx.repo.find("campus_tree_surveys", {"campus_id": campus["id"], "status": "DRAFT"})
    cases = await ctx.repo.find("review_cases", {"resource_id": campus["id"]})
    tree_audits = await ctx.repo.find("campus_tree_audits", {"campus_id": campus["id"], "review_status": "ACCEPTED"})
    return public("CampusResource", campus, eligible_area_ha=mask["eligible_area_ha"] if mask else None, pending_review_ids=[str(c["id"]) for c in cases if c["status"] != "CLOSED"], open_survey_ids=[str(s["id"]) for s in surveys], latest_accepted_tree_audit_id=str(tree_audits[0]["id"]) if tree_audits else None)


def controlled(campus):
    if campus["control_status"] != "APPROVED" or campus["control_input_hash"] != campus["control_approved_hash"] or campus["archived_at"]:
        fail(422, "CAMPUS_CONTROL_REQUIRED", "Current campus control must be independently approved.")


async def create(ctx, body):
    require_flow()
    if body["control_evidence_ids"]:
        fail(422, "EVIDENCE_PARENT_REQUIRED", "Create the pending campus before uploading campus-owned control evidence.")
    row = await ctx.repo.insert("campuses", {"institution_id": ctx.iid, **body, "created_by": ctx.actor["id"], "control_status": "PENDING", "control_input_hash": digest(body)})
    await ctx.repo.event(ctx.actor, "CAMPUS_CREATED", "campuses", row)
    ctx.revision = 1
    return await resource(ctx, row)


async def patch(ctx, body):
    require_flow()
    campus = ctx.parent
    ctx.match(campus)
    values = {key: body[key] for key in ("name", "control_evidence_ids") if key in body}
    if "address" in body:
        values["address"] = {**campus["address"], **body["address"]}
    values = {key: value for key, value in values.items() if normalize(campus.get(key)) != normalize(value)}
    if not values and ("archive" not in body or bool(campus["archived_at"]) == body["archive"]):
        fail(422, "NO_CHANGES", "At least one campus field must change.")
    if body.get("archive") is True:
        values.update(control_status="ARCHIVED", archived_at=datetime.now(timezone.utc))
    changed = bool(set(values) & {"name", "address", "control_evidence_ids"}) or body.get("archive") is False
    if changed:
        ids = list(map(str, values.get("control_evidence_ids", campus["control_evidence_ids"])))
        manifest = await evidence(ctx.repo, ids, "CAMPUS", campus["id"], ctx.iid)
        values.update(control_status="PENDING", control_approved_hash=None, control_input_hash=digest({"name": values.get("name", campus["name"]), "address": values.get("address", campus["address"]), "evidence": manifest}), current_mask_id=None, current_plot_set_id=None)
        if body.get("archive") is False:
            values.update(archived_at=None, current_boundary_id=None)
    row = await ctx.repo.update("campuses", campus["id"], values, bump=True)
    if changed and row["control_evidence_ids"]:
        case = await case_for(ctx.repo, ctx.actor, "campuses", row, "CAMPUS_CONTROL", await resource(ctx, row))
        row = await ctx.repo.update("campuses", row["id"], {"control_review_case_id": case["id"]})
    await ctx.repo.event(ctx.actor, "CAMPUS_CHANGED", "campuses", row)
    ctx.revision = row["input_revision"]
    return await resource(ctx, row)


async def boundary(ctx, body):
    require_flow()
    campus = ctx.parent
    ctx.match(campus)
    if body["source"] != "UPLOADED" and not (body["source"] == "OPERATOR_DIGITIZED" and getattr(ctx, "operator_digitization", False)):
        fail(403, "PERMISSION_DENIED", "Operator digitization must use scoped independent tooling.")
    geometry, area = await measure(ctx.repo, body["geometry"])
    manifest = await evidence(ctx.repo, body["evidence_ids"], "CAMPUS", campus["id"], ctx.iid, required=True)
    prior = await ctx.repo.find("campus_boundaries", {"campus_id": campus["id"]})
    predecessor = body.get("correction_of_boundary_id")
    if predecessor and predecessor not in list(map(lambda r: str(r["id"]), prior)):
        fail(404, "RESOURCE_NOT_FOUND", "Boundary predecessor is unavailable.")
    row = await ctx.repo.insert("campus_boundaries", {"institution_id": ctx.iid, "campus_id": campus["id"], **body, "geometry": geometry, "geometry_hash": digest(geometry), "area_ha": area, "revision": max([r["revision"] for r in prior] or [0]) + 1, "created_by": ctx.actor["id"], "review_status": "PENDING_REVIEW", "input_hash": digest({**body, "geometry": geometry, "evidence": manifest, "control_input_hash": campus["control_input_hash"]})})
    await case_for(ctx.repo, ctx.actor, "campus_boundaries", row, "BOUNDARY")
    campus = await ctx.repo.update("campuses", campus["id"], {}, bump=True)
    ctx.revision = campus["input_revision"]
    overlaps = await ctx.repo.rows("SELECT id FROM campus_boundaries WHERE id<>:id AND review_status='APPROVED' AND ST_Intersects(geometry,ST_SetSRID(ST_GeomFromGeoJSON(:g),4326)) LIMIT 100", {"id": row["id"], "g": __import__("json").dumps(geometry)})
    farm = await ctx.repo.rows("SELECT id FROM land_parcels WHERE ST_Intersects(geom,ST_SetSRID(ST_GeomFromGeoJSON(:g),4326)) LIMIT 100", {"g": __import__("json").dumps(geometry)})
    return public("BoundaryResource", row, overlap_flags=["OVERLAP_REVIEW_REQUIRED"] if overlaps or farm else [])


async def lineage(ctx, campus, body, *, plots=False):
    controlled(campus)
    boundary = await ctx.repo.get("campus_boundaries", body["boundary_id"])
    if str(boundary["campus_id"]) != str(campus["id"]) or boundary["review_status"] != "APPROVED" or str(campus["current_boundary_id"]) != str(boundary["id"]):
        fail(422, "APPROVED_BOUNDARY_REQUIRED", "Select the current approved campus boundary.")
    if plots:
        mask = await ctx.repo.get("campus_vegetation_masks", body["mask_id"])
        if str(mask["campus_id"]) != str(campus["id"]) or str(mask["boundary_id"]) != str(boundary["id"]) or str(campus["current_mask_id"]) != str(mask["id"]) or mask["review_status"] != "APPROVED" or mask["processing_status"] != "SUCCEEDED":
            fail(422, "APPROVED_MASK_REQUIRED", "Select the compatible approved tree-bearing mask.")
        return boundary, mask
    return boundary


async def request_mask(ctx, body):
    require_flow()
    ctx.match(ctx.parent)
    boundary = await lineage(ctx, ctx.parent, body)
    method = await approved(ctx.repo, "carbon_methodologies", body["method_version"])
    manifests = await evidence(ctx.repo, body["evidence_ids"], "CAMPUS", ctx.parent["id"], ctx.iid)
    previous = await ctx.repo.find("campus_vegetation_masks", {"campus_id": ctx.parent["id"]})
    if body.get("correction_of_mask_id") and body["correction_of_mask_id"] not in [str(r["id"]) for r in previous]:
        fail(404, "RESOURCE_NOT_FOUND", "Mask predecessor is unavailable.")
    row = await ctx.repo.insert("campus_vegetation_masks", {"institution_id": ctx.iid, "campus_id": ctx.parent["id"], **body, "revision": max([r["revision"] for r in previous] or [0]) + 1, "created_by": ctx.actor["id"], "parameters": {}, "coverage": {}, "input_hash": digest({**body, "boundary": boundary["input_hash"], "method": method["artifact_hash"], "evidence": manifests}), "processing_status": "QUEUED", "review_status": "PENDING_REVIEW"})
    job = await enqueue(ctx, "campus_mask", row, {"input_hash": row["input_hash"], "campus": normalize(ctx.parent), "boundary": normalize(boundary), "method": normalize(method), "request": body}, asset_type="MASK")
    campus = await ctx.repo.update("campuses", ctx.parent["id"], {}, bump=True)
    ctx.revision = campus["input_revision"]
    return accepted(job)


async def request_plots(ctx, body):
    require_flow()
    ctx.match(ctx.parent)
    boundary, mask = await lineage(ctx, ctx.parent, body, plots=True)
    policy = await ctx.repo.get("method_quality_policies", body["quality_policy_version"])
    method = await approved(ctx.repo, "carbon_methodologies", policy["methodology_id"])
    previous = await ctx.repo.find("campus_plot_sets", {"campus_id": ctx.parent["id"]})
    if body.get("correction_of_plot_set_id") and body["correction_of_plot_set_id"] not in [str(r["id"]) for r in previous]:
        fail(404, "RESOURCE_NOT_FOUND", "Plot-set predecessor is unavailable.")
    jid = str(uuid4())
    row = await ctx.repo.insert("campus_plot_sets", {"institution_id": ctx.iid, "campus_id": ctx.parent["id"], **body, "revision": max([r["revision"] for r in previous] or [0]) + 1, "created_by": ctx.actor["id"], "design": policy["config"]["sampling"], "review_status": "PENDING_REVIEW", "input_hash": digest({**body, "boundary_hash": boundary["input_hash"], "mask_hash": mask["input_hash"], "policy_hash": policy["artifact_hash"]}), "generation_job_id": jid})
    job = await enqueue(ctx, "campus_plot_generation", row, {"input_hash": row["input_hash"], "boundary": normalize(boundary), "mask": normalize(mask), "policy": normalize(policy), "method": normalize(method), "campus": normalize(ctx.parent)}, asset_type="PLOT_SET", job_id=jid)
    campus = await ctx.repo.update("campuses", ctx.parent["id"], {}, bump=True)
    ctx.revision = campus["input_revision"]
    return accepted(job)


async def assessment_config(ctx, campus):
    controlled(campus)
    from services.institution.runtime_configuration import current_method
    method_id = ctx.request.query_params.get("method_version")
    method = await approved(ctx.repo, "carbon_methodologies", method_id) if method_id else await current_method(ctx.repo, ctx.actor)
    policies = await ctx.repo.find("method_quality_policies", {"methodology_id": method["id"]})
    if not policies:
        fail(503, "METHOD_NOT_CONFIGURED", "The campus sampling and measurement policy is not published.")
    policy = policies[0]
    survey_id = ctx.request.query_params.get("survey_id")
    survey, grant = None, None
    boundary_id, mask_id, plot_set_id = campus["current_boundary_id"], campus["current_mask_id"], campus["current_plot_set_id"]
    if survey_id:
        survey = await ctx.repo.get("campus_tree_surveys", survey_id)
        if str(survey["campus_id"]) != str(campus["id"]) or survey["status"] not in {"DRAFT", "ACCEPTED"}:
            fail(404, "RESOURCE_NOT_FOUND", "Survey preparation is unavailable.")
        boundary_id, mask_id, plot_set_id = survey["boundary_id"], survey["mask_id"], survey["plot_set_id"]
        policy = await ctx.repo.get("method_quality_policies", survey["quality_policy_version"])
        if str(policy["methodology_id"]) != str(method["id"]):
            if ctx.request.query_params.get("method_version"):
                fail(422, "INCOMPATIBLE_METHOD", "Preparation must use the frozen survey's quality-policy method.")
            method = await approved(ctx.repo, "carbon_methodologies", policy["methodology_id"])
    parameters = await ctx.repo.find("method_parameters", {"methodology_id": method["id"]})
    actions, requirements = [], []
    if boundary_id and mask_id:
        actions.append("GENERATE_PLOTS")
    if plot_set_id:
        actions.append("START_SURVEY")
    if survey and survey["status"] == "DRAFT":
        issued = datetime.now(timezone.utc)
        raw = {"firebase_uid": ctx.actor["firebase_uid"], "institution_id": ctx.iid, "permission": ctx.member["permission"], "campus_id": str(campus["id"]), "survey_id": str(survey["id"]), "boundary_id": str(boundary_id), "mask_id": str(mask_id), "plot_set_id": str(plot_set_id), "quality_policy_version": str(policy["id"]), "issued_at": normalize(issued), "expires_at": normalize(issued + timedelta(seconds=settings.INSTITUTION_OFFLINE_MAX_SECONDS))}
        grant = {**raw, "context_hash": digest(raw)}
    from services.institution.farmer_biomass import choice, MODEL_ID
    model_id = ctx.request.query_params.get("model_version")
    if model_id and model_id != MODEL_ID:
        fail(422, "MODEL_RUNTIME_MISMATCH", "New assessments use the shared farmer training implementation.")
    registered = await ctx.repo.get("model_registry", MODEL_ID, required=False)
    models = [choice()] if registered is None or registered["status"] == "ACTIVE" else []
    if models and survey and survey["status"] == "ACCEPTED":
        actions.append("REQUEST_TREE_AUDIT")
    if not models:
        requirements.append({"path": "/model_version", "code": "MODEL_NOT_CONFIGURED", "message": "No approved applicable campus model is available."})
    if not boundary_id or not mask_id:
        fail(422, "ASSESSMENT_NOT_READY", "Approve the current boundary and eligible vegetation mask before preparing a survey.")
    capture_parameters = [{"id": str(p["id"]), "kind": p["kind"], "value": normalize(p["value"]), "unit": p["unit"], "formula": p["formula_key"], "applicability": p["applicability"], "source_metadata": p["source_metadata"]} for p in parameters]
    from services.institution.factor_service import compile_schema
    species_catalog = []
    for code in policy["config"]["eligibility"]["species_codes"]:
        sources = {"ALLOMETRY": [], "WOOD_DENSITY": []}
        for parameter in parameters:
            if parameter["kind"] not in sources:
                continue
            if parameter["kind"] == "ALLOMETRY" and str(parameter["id"]) not in list(map(str, policy["config"]["eligibility"]["allometry_ids"])):
                continue
            validator = compile_schema(parameter["applicability"])
            rule = parameter["applicability"].get("properties", {}).get("species_code", {})
            if not list(validator.descend(code, rule)):
                sources[parameter["kind"]].append(str(parameter["id"]))
        species_catalog.append({"code": code, "name": code, "native_label": None, "allometry_id": sources["ALLOMETRY"][0] if len(sources["ALLOMETRY"]) == 1 else None, "wood_density_source_id": sources["WOOD_DENSITY"][0] if len(sources["WOOD_DENSITY"]) == 1 else None})
    # Return the literal typed fields defined by AssessmentConfig.
    return {"campus_id": str(campus["id"]), "boundary_id": str(boundary_id), "mask_id": str(mask_id), "plot_set_id": str(plot_set_id) if plot_set_id else None, "method_version": str(method["id"]), "quality_policy_version": str(policy["id"]), "quality_policy": policy["config"], "parameters": capture_parameters, "applicable_models": models, "offline_context": grant, "enabled_actions": actions, "map_artifact_ids": [], "plot_artifact_ids": [], "species_catalog": species_catalog, "capabilities": {"generate_plots": "GENERATE_PLOTS" in actions, "start_survey": "START_SURVEY" in actions, "capture_measurements": grant is not None, "request_tree_audit": "REQUEST_TREE_AUDIT" in actions}, "source_versions": [{"id": str(method["id"]), "artifact_hash": method["artifact_hash"]}, {"id": str(policy["id"]), "artifact_hash": policy["artifact_hash"]}], "requirements": requirements}
