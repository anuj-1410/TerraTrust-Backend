"""Domain-neutral inference boundary. Contains no tenant authorization or minting."""
from __future__ import annotations

from decimal import Decimal, localcontext
import hashlib
import json
import math

from app.institution_errors import fail
from models.institution.common import normalize
from services.shared.allometry_service import tree_biomass
from services.shared.satellite_service import features
from services.shared.carbon_stock_service import stock


def ground_labels(survey):
    totals = {}
    parameters = survey["parameters"]
    for row in survey["measurements"]:
        if row["validation_status"] != "VALID":
            fail(422, "SURVEY_NEEDS_DATA", "A frozen observation is not eligible for biomass inference.")
        plot = str(row["plot_id"])
        totals[plot] = totals.get(plot, Decimal(0)) + tree_biomass(row["raw_input"], parameters)
    return totals


def infer_pixels(frozen, artifact_bytes):
    import ee
    from app.gee import ensure_gee_initialized
    ensure_gee_initialized()
    config = frozen["model"]["config"]
    if hashlib.sha256(artifact_bytes).hexdigest() != config["artifact_sha256"]:
        fail(422, "MODEL_HASH_MISMATCH", "Frozen model bytes do not match the reviewed artifact.")
    schema = config["feature_schema"]
    if schema["aggregation"] != "EXACT_PIXEL_INTERSECTION_EPSG6933_V1":
        fail(422, "UNSUPPORTED_AGGREGATION", "Commission the exact supported eligible-pixel aggregation version.")
    image, source_manifest = features(frozen, with_provenance=True)
    resolution = float(config["spatial_resolution_m"])
    projection = ee.Projection("EPSG:6933").atScale(resolution)
    eligible = ee.Geometry(frozen["mask"]["eligible_geometry"])
    max_pixels = config["inference_limits"]["max_tile_pixels"]
    if float(frozen["boundary"]["area_ha"]) * 10000 / (resolution * resolution) > max_pixels:
        fail(422, "INFERENCE_TILE_LIMIT", "Campus requires separately commissioned tiled inference.")
    coordinates = ee.Image.pixelCoordinates(projection)
    columns = schema["ordered_names"] + ["x", "y"]
    if config["engine"] == "GEE_SMILE_GBT":
        descriptor = json.loads(artifact_bytes)
        if set(descriptor) != {"schema_version", "engine", "asset_id", "asset_update_time", "feature_schema_version"} or descriptor["schema_version"] != 1 or descriptor["engine"] != "GEE_SMILE_GBT" or descriptor["feature_schema_version"] != config["feature_schema_version"]:
            fail(422, "MODEL_DESCRIPTOR_INVALID", "GEE model descriptor differs from the reviewed schema.")
        metadata = ee.data.getAsset(descriptor["asset_id"])
        if metadata.get("updateTime") != descriptor["asset_update_time"]:
            fail(422, "MODEL_ASSET_CHANGED", "The GEE model asset changed since independent validation.")
        image = image.addBands(image.classify(ee.Classifier.load(descriptor["asset_id"])).rename("AGB_T_HA"))
        columns.append("AGB_T_HA")
    elif config["engine"] != "XGBOOST":
        fail(422, "UNSUPPORTED_MODEL", "This reviewed engine is unsupported.")
    sampled = image.addBands(coordinates).sample(region=eligible.buffer(resolution * math.sqrt(2)), projection=projection, scale=resolution, geometries=False, dropNulls=True).select(columns).getInfo()
    values = [feature["properties"] for feature in sampled["features"]]
    if not values or len(values) > max_pixels:
        fail(422, "SATELLITE_NEEDS_DATA", "Required feature coverage is absent or exceeds the approved resource limit.")
    if config["engine"] == "XGBOOST":
        import numpy as np
        import xgboost as xgb
        if config["engine_version"] != xgb.__version__:
            fail(422, "MODEL_RUNTIME_MISMATCH", "The runtime differs from the reviewed XGBoost model version.")
        model = xgb.Booster()
        model.load_model(bytearray(artifact_bytes))
        if model.feature_names and model.feature_names != schema["ordered_names"]:
            fail(422, "FEATURE_SCHEMA_MISMATCH", "Model feature order differs from the frozen schema.")
        predictions = model.predict(xgb.DMatrix(np.array([[row[name] for name in schema["ordered_names"]] for row in values]), feature_names=schema["ordered_names"]))
        for row, prediction in zip(values, predictions):
            row["AGB_T_HA"] = float(prediction)
    if any(not math.isfinite(row["AGB_T_HA"]) or row["AGB_T_HA"] < 0 for row in values):
        fail(422, "MODEL_OUTPUT_INVALID", "Predicted biomass density is nonfinite or negative.")
    return values, projection.getInfo()["transform"], source_manifest


async def integrate(repo, frozen, pixels, transform, source_manifest=None):
    import json
    with localcontext() as ctx:
        ctx.prec = 50
        total, covered, output = Decimal(0), Decimal(0), []
        a, b, c, d, e, f = transform
        if b != 0 or d != 0:
            fail(422, "UNSUPPORTED_GRID", "Rotated raster grids require a reviewed adapter.")
        for pixel in pixels:
            x, y = pixel["x"], pixel["y"]
            x0, x1, y0, y1 = a * x + c, a * (x + 1) + c, e * y + f, e * (y + 1) + f
            row = await repo.one("SELECT ST_Area(ST_Intersection(ST_Transform(ST_MakeEnvelope(:x0,:y0,:x1,:y1,6933),4326),ST_SetSRID(ST_GeomFromGeoJSON(:mask),4326))::geography)/10000 AS area", {"x0": min(x0, x1), "x1": max(x0, x1), "y0": min(y0, y1), "y1": max(y0, y1), "mask": json.dumps(frozen["mask"]["eligible_geometry"])})
            area = Decimal(str(row["area"]))
            covered += area
            density = Decimal(str(pixel["AGB_T_HA"]))
            total += density * area
            output.append({"x": x, "y": y, "agb_t_ha": str(density), "eligible_pixel_area_ha": str(area)})
        policy = frozen["survey"]["policy"]["config"]
        eligible_area = Decimal(str(frozen["mask"]["eligible_area_ha"]))
        coverage_ratio = covered / eligible_area
        if coverage_ratio < Decimal(policy["validation"]["minimum_coverage_ratio"]) or eligible_area < Decimal(policy["validation"]["minimum_eligible_area_ha"]):
            fail(422, "SATELLITE_NEEDS_DATA", "Campus area or actual sensor/pixel support fails the approved policy.")
        fractions = [p for p in frozen["method"]["config"]["parameters"] if p["kind"] == "CARBON_FRACTION"]
        if len(fractions) != 1 or fractions[0]["value"] is None:
            fail(422, "CARBON_FRACTION_REQUIRED", "Select one exact reviewed carbon fraction.")
        result = stock(total, Decimal(fractions[0]["value"]), frozen)
        labels = ground_labels(frozen["survey"])
        plots = await repo.find("campus_sampling_plots", {"plot_set_id": frozen["survey"]["survey"]["plot_set_id"]})
        labels = {str(plot["id"]): normalize(labels[str(plot["id"])] / 1000 / Decimal(str(plot["measured_area_ha"]))) for plot in plots if str(plot["id"]) in labels}
        result.update(coverage={"eligible_area_ha": normalize(eligible_area), "covered_area_ha": normalize(covered), "coverage_ratio": normalize(coverage_ratio), "native_resolution_m": frozen["model"]["config"]["feature_schema"]["native_resolution_m"]}, provenance={"domain": "INSTITUTION", "algorithm": frozen["model"]["config"]["engine"], "engine_version": frozen["model"]["config"]["engine_version"], "model_artifact_sha256": frozen["model"]["config"]["artifact_sha256"], "feature_schema": frozen["model"]["config"]["feature_schema"], "quality_policy_version": frozen["survey"]["policy"]["id"], "plot_labels_tAGB_ha": labels, "pixel_grid": {"crs": "EPSG:6933", "transform": transform}, "events": frozen.get("events", []), "pools": frozen["method"]["config"]["pools"]})
        result["provenance"]["tree_ids"] = sorted(str(tree["id"]) for tree in frozen["survey"]["trees"])
        result["provenance"]["satellite_sources"] = source_manifest or {}
        result["provenance"]["plot_set_id"] = str(frozen["survey"]["survey"]["plot_set_id"])
        result["provenance"]["quality_policy_hash"] = frozen["survey"]["policy"]["artifact_hash"]
        result["provenance"]["observation_window"] = {key: frozen["request"][key] for key in ("observation_start", "observation_end")}
        return result, {"type": "TerraTrustBiomassGrid", "crs": "EPSG:6933", "transform": transform, "pixels": output, "units": "tAGB/ha", "boundary": frozen["boundary"]["geometry"], "eligible_geometry": frozen["mask"]["eligible_geometry"]}
