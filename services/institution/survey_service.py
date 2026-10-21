"""Stable tree inventory, raw observations, reviewed surveys and biomass jobs."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from uuid import uuid4
import json

from sqlalchemy import text

from app.institution_access import require_flow
from app.institution_errors import fail
from models.institution.common import digest, normalize, measurement_hash
from models.institution.contracts import parse
from services.institution.base import public, draft, evidence, link_evidence, case_for
from services.institution.campus_service import controlled, lineage
from services.institution.factor_service import approved, compile_schema
from services.institution.job_service import enqueue, accepted
from services.shared.allometry_service import dbh, tree_biomass
from services.shared.sampling_service import point


def tree_audit_resource(row):
    return public("TreeAuditResource", row, change=row.get("signed_change_tCO2"), stock_gain=row.get("stock_gain_tCO2"), stock_loss=row.get("stock_loss_tCO2"), removal=row.get("removal_tCO2"), release=row.get("release_tCO2"), source_refs=[str(x) for x in row.get("attribution_parameter_ids", [])])


async def create_tree(ctx, body):
    require_flow()
    controlled(ctx.parent)
    point(body["location"])
    await location_in_campus(ctx, body["location"])
    if body.get("planting_date") and body["planting_date"] > datetime.now(timezone.utc).date().isoformat():
        fail(422, "FUTURE_TREE_EVENT", "Planting date must describe an actual event.")
    row = await ctx.repo.insert("campus_trees", {"institution_id": ctx.iid, "campus_id": ctx.parent["id"], **body, "created_by": ctx.actor["id"]})
    await ctx.repo.event(ctx.actor, "TREE_CREATED", "campus_trees", row)
    ctx.revision = row["input_revision"]
    return public("TreeResource", row)


async def location_in_campus(ctx, location):
    boundary_id = ctx.parent.get("current_boundary_id")
    if not boundary_id:
        fail(422, "APPROVED_BOUNDARY_REQUIRED", "Approve the campus boundary before recording tree locations.")
    result = await ctx.repo.one("SELECT ST_Covers(geometry,ST_SetSRID(ST_GeomFromGeoJSON(:location),4326)) AS inside FROM campus_boundaries WHERE id=:id AND campus_id=:cid AND review_status='APPROVED'", {"id": boundary_id, "cid": ctx.parent["id"], "location": json.dumps(location)}, required=False)
    if not result or not result["inside"]:
        fail(422, "TREE_OUTSIDE_CAMPUS", "Tree location must lie inside the current approved campus boundary.")


async def patch_tree(ctx, body):
    require_flow()
    tree = await ctx.repo.get("campus_trees", ctx.request.path_params["tid"], lock=True)
    if str(tree["campus_id"]) != str(ctx.parent["id"]):
        fail(404, "RESOURCE_NOT_FOUND", "Tree is unavailable.")
    ctx.match(tree)
    changes = {key: value for key, value in body.items() if key != "event" and normalize(tree.get(key)) != value}
    if not changes and not body.get("event"):
        fail(422, "NO_CHANGES", "At least one tree field must change.")
    if "location" in changes:
        point(changes["location"])
        await location_in_campus(ctx, changes["location"])
    event = body.get("event")
    if set(changes) & {"stable_tag", "condition", "location", "planting_date"} and not event:
        fail(422, "TREE_EVENT_REQUIRED", "Record the dated reason/evidence for this tree change.")
    if event:
        if event["date"] > datetime.now(timezone.utc).date().isoformat():
            fail(422, "FUTURE_TREE_EVENT", "Tree events must record actual past or current dates.")
        await evidence(ctx.repo, event["evidence_ids"], "CAMPUS", ctx.parent["id"], ctx.iid)
        expected = {"DIED": {"condition": "DEAD"}, "REMOVED": {"condition": "REMOVED"}}
        if event["type"] in expected and any(changes.get(key) != value for key, value in expected[event["type"]].items()):
            fail(422, "TREE_EVENT_MISMATCH", "Condition must agree with the recorded mortality/removal event.")
        await ctx.repo.insert("campus_tree_events", {"institution_id": ctx.iid, "campus_id": ctx.parent["id"], "tree_id": tree["id"], "event_type": event["type"], "event_date": event["date"], "previous_values": normalize({key: tree[key] for key in changes}), "new_values": changes, "metadata": {}, "reason": event["reason"], "evidence_ids": event["evidence_ids"], "actor_id": ctx.actor["id"]})
    saved = await ctx.repo.update("campus_trees", tree["id"], changes, bump=True)
    await ctx.repo.event(ctx.actor, "TREE_UPDATED", "campus_trees", saved)
    ctx.revision = saved["input_revision"]
    return public("TreeResource", saved)


async def create_survey(ctx, body):
    require_flow()
    campus = ctx.parent
    boundary, mask = await lineage(ctx, campus, body, plots=True)
    plot_set = await ctx.repo.get("campus_plot_sets", body["plot_set_id"])
    if str(plot_set["campus_id"]) != str(campus["id"]) or str(plot_set["mask_id"]) != str(mask["id"]) or str(plot_set["boundary_id"]) != str(boundary["id"]) or str(plot_set["quality_policy_version"]) != body["quality_policy_version"] or plot_set["review_status"] != "APPROVED":
        fail(422, "APPROVED_PLOTS_REQUIRED", "Survey requires the exact approved campus plot set and policy.")
    row = await ctx.repo.insert("campus_tree_surveys", {"institution_id": ctx.iid, "campus_id": campus["id"], **body, "logical_survey_id": str(uuid4()), "revision": 1, "created_by": ctx.actor["id"], "status": "DRAFT", "input_hash": digest(body), "sync_validation": {}})
    await ctx.repo.event(ctx.actor, "SURVEY_CREATED", "campus_tree_surveys", row)
    ctx.revision = row["input_revision"]
    return await survey_resource(ctx, row)


async def observation_quality(ctx, survey, raw):
    issues = []
    def missing(key, message, code="NEEDS_DATA"):
        issues.append({"path": "/" + key, "code": code, "message": message})
    policy = await ctx.repo.get("method_quality_policies", survey["quality_policy_version"])
    config = policy["config"]
    tree = await ctx.repo.get("campus_trees", raw["tree_id"])
    plot = await ctx.repo.get("campus_sampling_plots", raw["plot_id"])
    if str(tree["campus_id"]) != str(survey["campus_id"]) or str(plot["plot_set_id"]) != str(survey["plot_set_id"]):
        fail(404, "RESOURCE_NOT_FOUND", "Tree or plot is unavailable in this survey.")
    measured = datetime.fromisoformat(raw["measured_at"].replace("Z", "+00:00"))
    if measured > datetime.now(timezone.utc) + timedelta(minutes=5):
        fail(422, "FUTURE_MEASUREMENT", "Measurement time exceeds the allowed device clock skew.")
    if not str(survey["survey_start"]) <= measured.date().isoformat() <= str(survey["survey_end"]):
        missing("measured_at", "Observation must lie within the approved survey dates.")
    if raw["protocol_version"] != config["measurement_protocol_version"]:
        missing("protocol_version", "The capture protocol does not match the survey policy.")
    if raw["location"] is None:
        missing("location", "Capture actual GPS before submission.", "GPS_NOT_CAPTURED")
    else:
        point(raw["location"])
        geo = json.dumps(raw["location"])
        result = await ctx.repo.one("SELECT ST_Covers(ST_Buffer(p.geometry::geography,:tolerance)::geometry,ST_SetSRID(ST_GeomFromGeoJSON(:geo),4326)) AS inside FROM campus_sampling_plots p WHERE p.id=:id", {"id": plot["id"], "geo": geo, "tolerance": float(config["gps"]["plot_tolerance_m"])})
        if not result["inside"]:
            missing("location", "Observation lies outside its measured plot.")
        if raw["gps_accuracy_m"] is None or Decimal(raw["gps_accuracy_m"]) > Decimal(config["gps"]["max_accuracy_m"]):
            missing("gps_accuracy_m", "GPS accuracy does not meet the approved campus policy.")
    manifests = await evidence(ctx.repo, raw["evidence_ids"], "SURVEY", survey["id"], ctx.iid)
    photos = [await ctx.repo.get("institution_audit_evidence", item["id"]) for item in manifests]
    if sum(photo["mime_type"] in {"image/jpeg", "image/png"} for photo in photos) < config["evidence"]["minimum_photos"]:
        missing("evidence_ids", "Capture the evidence required by the approved measurement policy.")
    diameter = dbh(raw)
    height = Decimal(raw["height_m"]) if raw["height_m"] else None
    if diameter is None:
        missing("dbh_cm", "A supported DBH measurement/protocol is required.")
    elif not Decimal(config["dimensions"]["dbh_min_cm"]) <= diameter <= Decimal(config["dimensions"]["dbh_max_cm"]):
        missing("dbh_cm", "DBH is outside the approved equation range.")
    if height is None or raw["height_source"] == "UNKNOWN":
        missing("height_m", "Supported height provenance is required.")
    elif not Decimal(config["dimensions"]["height_min_m"]) <= height <= Decimal(config["dimensions"]["height_max_m"]):
        missing("height_m", "Height is outside the approved equation range.")
    if raw["species_code"] not in config["eligibility"]["species_codes"]:
        missing("species_code", "Species is not supported by this reviewed campus method.")
    if raw["allometry_id"] and raw["allometry_id"] not in list(map(str, config["eligibility"]["allometry_ids"])):
        missing("allometry_id", "Select an equation explicitly allowed by this survey policy.")
    if Decimal(raw["measurement_height_m"]) != Decimal(config["dimensions"]["reference_height_m"]):
        missing("measurement_height_m", "This implemented capture protocol requires the reviewed reference height.")
    threshold = config["species"]["minimum_classifier_confidence"]
    if raw["species_selection"] == "CLASSIFIER" and (raw["species_confidence"] is None or threshold is None or Decimal(raw["species_confidence"]) < Decimal(threshold)):
        missing("species_confidence", "Classifier proposal needs supported species confirmation.")
    parameters = {}
    for key, kind in (("allometry_id", "ALLOMETRY"), ("wood_density_source_id", "WOOD_DENSITY")):
        if raw[key] is None:
            missing(key, "A reviewed applicable source is required.")
            continue
        parameter = await ctx.repo.get("method_parameters", raw[key])
        if str(parameter["methodology_id"]) != str(policy["methodology_id"]) or parameter["kind"] != kind:
            fail(422, "INCOMPATIBLE_SOURCE", "Measurement sources must belong to the approved survey method.")
        facts = {"species_code": raw["species_code"], "dbh_cm": normalize(diameter), "height_m": raw["height_m"], "domain": "INSTITUTION"}
        if list(compile_schema(parameter["applicability"]).iter_errors(facts)):
            missing(key, "The selected source is outside its reviewed applicability.")
        parameters[str(parameter["id"])] = parameter
    if not issues:
        try:
            tree_biomass(raw, parameters)
        except ValueError as exc:
            missing("allometry_id", str(exc))
    return diameter, issues


def measurement_resource(row):
    if row["raw_input"] is None:
        fail(409, "RAW_CAPTURE_UNAVAILABLE", "Original capture bytes are unavailable; resurvey or create a reviewed correction.")
    derivation = {"derived_dbh_cm": normalize(row.get("derived_dbh_cm")), "formula_key": "CIRCUMFERENCE_TO_DBH" if row["measurement_form"] == "CIRCUMFERENCE" else None, "method_version": None, "validation_status": row["validation_status"], "validation_issues": row["validation_issues"]}
    return {**row["raw_input"], "client_content_hash": row["content_hash"], "id": str(row["id"]), "institution_id": str(row["institution_id"]), "survey_id": str(row["survey_id"]), "input_revision": row["input_revision"], "content_hash": row["content_hash"], "created_at": normalize(row["created_at"]), "updated_at": normalize(row["updated_at"]), "data_origin": row["data_origin"], "derivation": derivation}


async def survey_hash(ctx, survey):
    rows = await ctx.repo.find("campus_tree_measurements", {"survey_id": survey["id"], "deleted_at": None})
    source = {key: normalize(survey[key]) for key in ("id", "boundary_id", "mask_id", "plot_set_id", "quality_policy_version", "survey_start", "survey_end", "revision")}
    source["measurements"] = sorted([{"id": str(row["id"]), "hash": row["content_hash"]} for row in rows], key=lambda row: row["id"])
    saved = await ctx.repo.update("campus_tree_surveys", survey["id"], {"input_hash": digest(source)}, bump=True)
    await ctx.repo.event(ctx.actor, "SURVEY_INPUT_SAVED", "campus_tree_surveys", saved)
    ctx.revision = saved["input_revision"]
    return saved


async def batch(ctx, body):
    require_flow()
    survey = ctx.parent
    draft(survey)
    if body["client_operation_id"] != ctx.request.headers["Idempotency-Key"] or body["survey_revision"] != survey["revision"]:
        fail(409, "SURVEY_REVISION_CONFLICT", "Operation ID and exact survey business revision must agree.")
    existing, new, receipts = [], [], []
    for item in body["items"]:
        rows = await ctx.repo.find("campus_tree_measurements", {"institution_id": ctx.iid, "local_uuid": item["local_uuid"]})
        if rows:
            old = rows[0]
            if str(old["survey_id"]) != str(survey["id"]) or old["content_hash"] != item["client_content_hash"] or old["deleted_at"]:
                fail(409, "MEASUREMENT_CONTENT_CONFLICT", "This observation UUID already identifies different saved content.")
            receipts.append({"local_uuid": item["local_uuid"], "id": str(old["id"]), "content_hash": old["content_hash"], "disposition": "EXISTING"})
        else:
            new.append(item)
    if new:
        ctx.match(survey)
    issues = []
    for item in new:
        diameter, item_issues = await observation_quality(ctx, survey, item)
        raw = {key: value for key, value in item.items() if key != "client_content_hash"}
        row = await ctx.repo.insert("campus_tree_measurements", {"institution_id": ctx.iid, "campus_id": survey["campus_id"], "survey_id": survey["id"], **raw, "revision": 1, "last_survey_input_revision":survey["input_revision"]+1, "content_hash": item["client_content_hash"], "raw_input": raw, "derived_dbh_cm": normalize(diameter) if item["measurement_form"] == "CIRCUMFERENCE" else None, "validation_status": "NEEDS_DATA" if item_issues else "VALID", "validation_issues": item_issues, "created_by": ctx.actor["id"]})
        issues.extend(item_issues)
        receipts.append({"local_uuid": item["local_uuid"], "id": str(row["id"]), "content_hash": row["content_hash"], "disposition": "CREATED"})
    survey = await survey_hash(ctx, survey) if new else survey
    ctx.revision = survey["input_revision"]
    return {"items": receipts, "input_revision": ctx.revision, "validation_issues": issues}


async def survey_resource(ctx, row):
    observations = await ctx.repo.find("campus_tree_measurements", {"survey_id": row["id"], "deleted_at": None})
    plots = await ctx.repo.find("campus_sampling_plots", {"plot_set_id": row["plot_set_id"]})
    policy = await ctx.repo.get("method_quality_policies", row["quality_policy_version"])
    errors, progress = [], []
    for plot in plots:
        selected = [obs for obs in observations if str(obs["plot_id"]) == str(plot["id"])]
        eligible = [obs for obs in selected if obs["validation_status"] == "VALID"]
        item_errors = [issue for obs in selected for issue in obs["validation_issues"]]
        if not eligible:
            item_errors.append({"path": "/plots/" + str(plot["id"]), "code": "INSUFFICIENT_SAMPLES", "message": "This required plot has no eligible biomass observations."})
        errors.extend(item_errors)
        progress.append({"plot_id": str(plot["id"]), "measurement_count": len(selected), "eligible_count": len(eligible), "required_support": policy["config"]["validation"], "complete": not item_errors, "issues": item_errors})
    cases = await ctx.repo.find("review_cases", {"resource_id": row["id"], "resource_type": "SURVEY"})
    validation = {"input_revision": row["input_revision"], "errors": errors, "warnings": [], "can_submit": bool(plots) and not errors}
    return public("SurveyResource", row, plot_progress=progress, measurement_count=len(observations), sync_validation=validation, review_case_id=cases[0]["id"] if cases else None, offline_context_expires_at=None, copied_id_mappings=[])


async def submit(ctx, body):
    require_flow()
    survey = ctx.parent
    ctx.match(survey)
    draft(survey)
    if body["expected_input_hash"] != survey["input_hash"]:
        fail(409, "INPUT_HASH_CHANGED", "The survey changed before submission.")
    campus = await ctx.repo.get("campuses", survey["campus_id"])
    controlled(campus)
    check = await survey_resource(ctx, survey)
    if not check["sync_validation"]["can_submit"]:
        fail(422, "INSUFFICIENT_SAMPLES", "Complete the approved plot/observation policy before submission.", fields=check["sync_validation"]["errors"])
    await evidence(ctx.repo, body["evidence_ids"], "SURVEY", survey["id"], ctx.iid, required=True)
    observations = await ctx.repo.find("campus_tree_measurements", {"survey_id": survey["id"], "deleted_at": None})
    trees, parameters, manifests = [], {}, {}
    for obs in observations:
        raw = measurement_resource(obs)
        _, issues = await observation_quality(ctx, survey, obs["raw_input"])
        if issues:
            fail(422, "SURVEY_NEEDS_DATA", "Observation eligibility changed; correct the draft first.", fields=issues)
        if not set(map(str, obs["evidence_ids"])) <= set(body["evidence_ids"]):
            fail(422, "EVIDENCE_MISSING", "Submit all selected observation evidence.")
        trees.append(normalize(await ctx.repo.get("campus_trees", obs["tree_id"])))
        for pid in (obs["allometry_id"], obs["wood_density_source_id"]):
            parameters[str(pid)] = normalize(await ctx.repo.get("method_parameters", pid))
        for item in await evidence(ctx.repo, list(map(str, obs["evidence_ids"])), "SURVEY", survey["id"], ctx.iid):
            manifests[item["id"]] = item
    policy = await ctx.repo.get("method_quality_policies", survey["quality_policy_version"])
    snapshot = {"survey": normalize(survey), "trees": trees, "measurements": normalize(observations), "parameters": parameters, "policy": normalize(policy), "evidence": manifests, "campus": normalize(campus)}
    row = await ctx.repo.update("campus_tree_surveys", survey["id"], {"status": "SUBMITTED", "frozen_input_hash": survey["input_hash"], "frozen_snapshot": snapshot, "submitted_by": ctx.actor["id"], "submitted_at": datetime.now(timezone.utc)})
    await case_for(ctx.repo, ctx.actor, "campus_tree_surveys", row, "SURVEY", snapshot)
    ctx.revision = row["input_revision"]
    return await survey_resource(ctx, row)


async def request_audit(ctx, body):
    require_flow()
    ctx.match(ctx.parent)
    boundary, mask = await lineage(ctx, ctx.parent, body, plots=True)
    survey = await ctx.repo.get("campus_tree_surveys", body["survey_id"])
    if str(survey["campus_id"]) != str(ctx.parent["id"]) or str(survey["boundary_id"]) != body["boundary_id"] or str(survey["mask_id"]) != body["mask_id"] or survey["status"] != "ACCEPTED":
        fail(422, "ACCEPTED_SURVEY_REQUIRED", "Stock processing requires an independently accepted exact campus survey.")
    from services.institution import farmer_biomass
    if body["model_version"] != farmer_biomass.MODEL_ID:
        fail(422, "MODEL_RUNTIME_MISMATCH", "Refresh assessment configuration to use the shared farmer training implementation.")
    model = await farmer_biomass.model(ctx.repo, ctx.actor)
    method = await approved(ctx.repo, "carbon_methodologies", body["method_version"])
    if str(survey["frozen_snapshot"]["policy"]["methodology_id"]) != str(method["id"]):
        fail(422, "INCOMPATIBLE_METHOD", "The assessment must use its accepted survey's calculation configuration.")
    if body["feature_schema_version"] != model["config"]["feature_schema_version"]:
        fail(422, "FEATURE_SCHEMA_MISMATCH", "Select the exact activated model feature schema.")
    baseline = None
    if body["baseline_audit_id"]:
        baseline = await ctx.repo.get("campus_tree_audits", body["baseline_audit_id"])
        if str(baseline["campus_id"]) != str(ctx.parent["id"]) or baseline["review_status"] != "ACCEPTED":
            fail(422, "INCOMPATIBLE_BASELINE", "Select an accepted start assessment from this campus.")
    previous = await ctx.repo.find("campus_tree_audits", {"campus_id": ctx.parent["id"]})
    if body.get("correction_of_tree_audit_id") and body["correction_of_tree_audit_id"] not in [str(r["id"]) for r in previous]:
        fail(404, "RESOURCE_NOT_FOUND", "Assessment predecessor is unavailable.")
    events = []
    for eid in body.get("event_ids", []):
        event = await ctx.repo.get("campus_tree_events", eid)
        if str(event["campus_id"]) != str(ctx.parent["id"]):
            fail(404, "RESOURCE_NOT_FOUND", "Tree event is unavailable.")
        events.append(normalize(event))
    if baseline:
        required_events = await ctx.repo.rows("SELECT id FROM campus_tree_events WHERE campus_id=:cid AND event_date>:start AND event_date<=:end ORDER BY id", {"cid":ctx.parent["id"], "start":baseline["assessment_date"], "end":date.fromisoformat(body["assessment_date"])})
        if {str(row["id"]) for row in required_events} != {str(row["id"]) for row in events}:
            fail(422, "TREE_EVENT_COVERAGE_REQUIRED", "Include every recorded campus tree event in this comparison interval.")
    jid = str(uuid4())
    frozen = {"request": body, "campus": normalize(ctx.parent), "boundary": normalize(boundary), "mask": normalize(mask), "survey": survey["frozen_snapshot"], "model": normalize(model), "method": normalize(method), "baseline": normalize(baseline), "events": events}
    if model["config"].get("engine") == farmer_biomass.ENGINE or "NISAR" in model["config"].get("feature_schema", {}).get("required_sensors", []):
        from app.config import settings
        frozen["nisar_source"] = {"asset_id": settings.NISAR_GEE_ASSET_ID.strip(), "asset_type": settings.NISAR_GEE_ASSET_TYPE, "hh_band": settings.NISAR_HH_BAND, "hv_band": settings.NISAR_HV_BAND}
    if body["observation_end"] > datetime.now(timezone.utc).date().isoformat() or body["assessment_date"] > datetime.now(timezone.utc).date().isoformat():
        fail(422, "FUTURE_ASSESSMENT", "Stock assessments must use actual past or current observations.")
    from services.shared.model_applicability import validate as validate_model
    frozen["domain_facts"] = validate_model(frozen)
    farmer_biomass.scans(frozen)
    hashed = digest(frozen)
    row = await ctx.repo.insert("campus_tree_audits", {"institution_id": ctx.iid, "campus_id": ctx.parent["id"], **body, "revision": max([r["revision"] for r in previous] or [0]) + 1, "created_by": ctx.actor["id"], "input_hash": hashed, "processing_status": "QUEUED", "review_status": "PENDING", "job_id": jid, "comparability_status": "BASELINE_ONLY" if baseline is None else "NONCOMPARABLE", "coverage": {}, "quality_flags": [], "uncertainty": {"type": "UNKNOWN", "lower": None, "upper": None, "confidence_level": None, "method_version": None, "dependence_assumptions": None, "sources": []}, "provenance": {}, "attribution_sources": [], "event_ids": body.get("event_ids", []), "attribution_parameter_ids": body.get("attribution_parameter_ids", [])})
    job = await enqueue(ctx, "campus_fusion", row, {**frozen, "input_hash": hashed}, asset_type="TREE_AUDIT", job_id=jid)
    campus = await ctx.repo.update("campuses", ctx.parent["id"], {}, bump=True)
    ctx.revision = campus["input_revision"]
    return accepted(job)
