"""Frozen campus input adapter. Does not read farmer audits or issue tokens."""
from __future__ import annotations

import asyncio
from decimal import Decimal
import json
import math
import random
from uuid import uuid4

from app.institution_errors import fail
from models.institution.common import digest, normalize
from services.shared.sampling_service import polygon, measure


def mask_geometry(frozen):
    import ee
    from app.gee import ensure_gee_initialized
    ensure_gee_initialized()
    ee.data.setDeadline(120000)
    config = frozen["method"]["config"].get("campus_processing")
    if not config or config["mask_engine"] != "REVIEWED_GEE_MASK_ASSET_V1":
        fail(503, "METHOD_NOT_CONFIGURED", "A reviewed tree-bearing raster source and strata must be commissioned.")
    request = frozen["request"]
    if not request["observation_start"] <= config["mask_source_date"] <= request["observation_end"]:
        fail(422, "MASK_SOURCE_DATE_MISMATCH", "The reviewed mask source is outside the selected observation window.")
    asset = ee.data.getAsset(config["mask_asset_id"])
    if asset.get("updateTime") != config["mask_asset_update_time"]:
        fail(422, "MASK_SOURCE_CHANGED", "The reviewed mask asset changed after source approval.")
    region = ee.Geometry(frozen["boundary"]["geometry"])
    raster = ee.Image(config["mask_asset_id"]).select(0).eq(config["mask_tree_value"]).selfMask().toInt()
    resolution = float(config["mask_native_resolution_m"])
    if resolution <= 0:
        fail(422, "INVALID_MASK_RESOLUTION", "Mask resolution must be an actual positive sourced value.")
    vectors = raster.reduceToVectors(geometry=region.buffer(resolution * math.sqrt(2)), scale=resolution, geometryType="polygon", eightConnected=True, maxPixels=10_000_000).geometry().intersection(region, ee.ErrorMargin(0.1)).getInfo()
    geometry = polygon(vectors)
    return geometry, {"source_asset_id": config["mask_asset_id"], "source_update_time": config["mask_asset_update_time"], "source_date": config["mask_source_date"], "native_resolution_m": config["mask_native_resolution_m"], "method": config["mask_engine"], "excluded_classes": "All reviewed non-tree classes", "quality_flags": [], "fractional_boundary_rule": "EXACT_VECTOR_INTERSECTION_V1"}


async def generate_plots(repo, frozen):
    from services.institution.factor_service import compile_schema
    policy, mask = frozen["policy"]["config"], frozen["mask"]
    method = frozen["method"]
    processing = method["config"].get("campus_processing")
    if not processing or not processing["strata"]:
        fail(503, "METHOD_NOT_CONFIGURED", "Reviewed sampling stratum geometries and weights are required.")
    design = policy["sampling"]
    if design["design"] != "STRATIFIED_REVISITABLE" or design["inclusion_weight_rule"] != "REVIEWED_FIXED_PLOT_WEIGHTS_V1":
        fail(422, "UNSUPPORTED_SAMPLING_DESIGN", "This generation/weighting rule is not implemented.")
    required = design["required_plots_by_stratum"]
    if set(required) != {s["name"] for s in processing["strata"]}:
        fail(422, "STRATUM_POLICY_MISMATCH", "Reviewed strata must exactly match the sampling support policy.")
    radius = math.sqrt(float(design["plot_area_ha"]) * 10000 / math.pi)
    spacing = float(design["min_spacing_m"])
    generator = random.Random(design["seed"])
    accepted, rejected, centers = [], [], []
    for stratum in processing["strata"]:
        geometry = polygon(stratum["geometry"])
        probability, weight = Decimal(stratum["inclusion_probability"]), Decimal(stratum["design_weight"])
        if not 0 < probability <= 1 or weight <= 0:
            fail(422, "INVALID_SAMPLING_WEIGHT", "Use independently reviewed positive inclusion probabilities and weights.")
        data = await repo.one("WITH clipped AS (SELECT ST_Transform(ST_Intersection(ST_SetSRID(ST_GeomFromGeoJSON(:stratum),4326),ST_SetSRID(ST_GeomFromGeoJSON(:mask),4326)),6933) g) SELECT ST_XMin(g) xmin,ST_XMax(g) xmax,ST_YMin(g) ymin,ST_YMax(g) ymax,ST_AsGeoJSON(g)::jsonb geo FROM clipped", {"stratum": json.dumps(geometry), "mask": json.dumps(mask["eligible_geometry"])})
        if data["xmin"] is None or required[stratum["name"]] <= 0:
            fail(422, "INSUFFICIENT_SAMPLES", "A reviewed stratum has no eligible area/support.")
        selected = []
        for candidate in range(10000):
            if len(selected) >= required[stratum["name"]]:
                break
            x = generator.uniform(data["xmin"], data["xmax"])
            y = generator.uniform(data["ymin"], data["ymax"])
            if any(math.hypot(x - cx, y - cy) < max(spacing, radius * 2) for cx, cy in centers):
                rejected.append({"stratum": stratum["name"], "candidate": candidate, "reason": "SPACING"})
                continue
            result = await repo.one("WITH shape AS (SELECT ST_SetSRID(ST_GeomFromGeoJSON(:geo),6933) g), candidate AS (SELECT ST_Buffer(ST_Transform(ST_SetSRID(ST_MakePoint(:x,:y),6933),4326)::geography,:radius)::geometry g), clipped AS (SELECT ST_Intersection(ST_Transform(shape.g,4326),candidate.g) g,ST_Covers(ST_Transform(shape.g,4326),candidate.g) full FROM shape,candidate) SELECT NOT ST_IsEmpty(g) AS valid,full,ST_Area(g::geography)/10000 AS area,ST_AsGeoJSON(ST_Multi(ST_CollectionExtract(g,3)))::jsonb geo FROM clipped", {"geo": json.dumps(data["geo"]), "x": x, "y": y, "radius": radius})
            if not result["valid"] or result["area"] <= 0 or (design["edge_rule"] == "REQUIRE_FULL_PLOT" and not result["full"]):
                rejected.append({"stratum": stratum["name"], "candidate": candidate, "reason": "EDGE_OR_EMPTY"})
                continue
            centers.append((x, y))
            selected.append({"id": str(uuid4()), "geometry": result["geo"], "measured_area_ha": result["area"], "stratum": stratum["name"], "inclusion_rule": {"design": design["design"], "inclusion_probability": stratum["inclusion_probability"], "design_weight": stratum["design_weight"], "source_evidence_id": stratum["evidence_id"], "edge_rule": design["edge_rule"]}})
        if len(selected) != required[stratum["name"]]:
            fail(422, "INSUFFICIENT_SAMPLES", "Eligible geometry cannot support the reviewed plot area, spacing and stratum requirements.")
        accepted.extend(selected)
    return accepted, {"seed": design["seed"], "accepted_count": len(accepted), "rejected_candidates": rejected, "policy_hash": frozen["policy"]["artifact_hash"]}


async def compute_stock(repo, frozen):
    from services.institution.farmer_biomass import ENGINE, compute
    if frozen["model"]["config"].get("engine") == ENGINE:
        return await compute(repo, frozen)
    from services.shared.tree_carbon_engine import infer_pixels, integrate
    from services.shared.evidence_service import read_bytes
    from services.shared.model_applicability import validate as validate_model
    validate_model(frozen)
    artifact = await repo.get("institution_audit_evidence", frozen["model"]["config"]["artifact_id"])
    if artifact["state"] != "READY" or artifact["bytes_deleted_at"]:
        fail(422, "MODEL_ARTIFACT_UNAVAILABLE", "The reviewed model artifact is unavailable.")
    data = await read_bytes(artifact["private_bucket"], artifact["object_key"])
    pixels, transform, source_manifest = await asyncio.to_thread(infer_pixels, frozen, data)
    return await integrate(repo, frozen, pixels, transform, source_manifest)
