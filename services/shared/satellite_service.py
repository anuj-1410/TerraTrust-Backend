"""Institution-safe satellite facade using the inspected shared acquisition layer.

The farmer feature builder and its production behavior are deliberately untouched.
Institution reflectance, dB ratios, QA and observation dates are explicit.
"""
from datetime import date, timedelta

from app.institution_errors import fail

PREPROCESSING = {"S1_FOCAL_MEDIAN_3PX_V1", "S2_SCL_MASK_SR_V1", "GEDI_QUALITY_MASK_V1", "SRTM_V1"}
MAX_SOURCE_IMAGES = 10000


def pin_collection(ee, collection, dataset):
    """Resolve exact input IDs once and compose only those recorded images."""
    rows = collection.toList(MAX_SOURCE_IMAGES + 1).map(lambda item: ee.Dictionary({"asset_id": ee.Image(item).id(), "time_start_ms": ee.Image(item).get("system:time_start")})).getInfo()
    if not rows:
        fail(422, "SATELLITE_NEEDS_DATA", "A required sensor has no images in the selected window.")
    if len(rows) > MAX_SOURCE_IMAGES:
        fail(422, "SOURCE_IMAGE_LIMIT", "The selected window exceeds the supported imagery manifest size.")
    if any(not row.get("asset_id") or type(row.get("time_start_ms")) not in {int, float} for row in rows):
        fail(422, "SATELLITE_PROVENANCE_MISSING", "Actual image identities and acquisition dates are required.")
    rows.sort(key=lambda row: row["asset_id"])
    return ee.ImageCollection.fromImages([ee.Image(row["asset_id"]) for row in rows]), {"dataset": dataset, "images": rows}


def features(frozen, *, with_provenance=False):
    import ee
    from app.gee import ensure_gee_initialized
    from services.satellite_service import fetch_srtm_terrain
    ensure_gee_initialized()
    ee.data.setDeadline(120000)
    request, config = frozen["request"], frozen["model"]["config"]
    schema = config["feature_schema"]
    if set(schema["preprocessing"]) - PREPROCESSING:
        fail(422, "UNSUPPORTED_PREPROCESSING", "This model requires an unimplemented preprocessing version.")
    start = request["observation_start"]
    end = (date.fromisoformat(request["observation_end"]) + timedelta(days=1)).isoformat()
    import math
    region = ee.Geometry(frozen["boundary"]["geometry"]).buffer(float(config["spatial_resolution_m"]) * math.sqrt(2))
    names, sensors = schema["ordered_names"], set(schema["required_sensors"])
    bands, sources = {}, {}
    if "SENTINEL_1" in sensors:
        collection = ee.ImageCollection("COPERNICUS/S1_GRD").filterBounds(region).filterDate(start, end).filter(ee.Filter.eq("instrumentMode", "IW")).filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH")).filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
        collection, sources["SENTINEL_1"] = pin_collection(ee, collection, "COPERNICUS/S1_GRD")
        s1 = collection.select(["VH", "VV"]).median()
        if "S1_FOCAL_MEDIAN_3PX_V1" in schema["preprocessing"]:
            s1 = s1.focal_median(3, "square", "pixels")
        bands["S1_VH"] = s1.select("VH").rename("S1_VH")
        bands["S1_VV"] = s1.select("VV").rename("S1_VV")
        bands["S1_VH_VV_RATIO"] = ee.Image.constant(10).pow(bands["S1_VH"].subtract(bands["S1_VV"]).divide(10)).rename("S1_VH_VV_RATIO")
    if "SENTINEL_2" in sensors:
        collection = ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED").filterBounds(region).filterDate(start, end)
        collection, sources["SENTINEL_2"] = pin_collection(ee, collection, "COPERNICUS/S2_SR_HARMONIZED")
        def quality(image):
            scl = image.select("SCL")
            mask = scl.neq(0).And(scl.neq(1)).And(scl.neq(3)).And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10)).And(scl.neq(11))
            return image.updateMask(mask).select(["B2", "B4", "B5", "B7", "B8"]).multiply(0.0001)
        s2 = collection.map(quality).median()
        bands["NDVI"] = s2.normalizedDifference(["B8", "B4"]).rename("NDVI")
        bands["EVI"] = s2.expression("2.5*((n-r)/(n+6*r-7.5*b+1))", {"n": s2.select("B8"), "r": s2.select("B4"), "b": s2.select("B2")}).rename("EVI")
        bands["RED_EDGE"] = s2.select("B7").rename("RED_EDGE")
    if "GEDI" in sensors:
        def quality_gedi(image):
            return image.updateMask(image.select("quality_flag").eq(1)).updateMask(image.select("degrade_flag").eq(0)).select("rh98")
        collection = ee.ImageCollection("LARSE/GEDI/GEDI02_A_002_MONTHLY").filterBounds(region).filterDate(start, end)
        collection, sources["GEDI"] = pin_collection(ee, collection, "LARSE/GEDI/GEDI02_A_002_MONTHLY")
        bands["GEDI_RH98"] = collection.map(quality_gedi).mean().rename("GEDI_RH98")
    if "SRTM" in sensors:
        elevation, slope = fetch_srtm_terrain(region)
        bands.update(ELEVATION=elevation, SLOPE=slope)
        sources["SRTM"] = {"dataset": "USGS/SRTMGL1_003", "asset": ee.data.getAsset("USGS/SRTMGL1_003")}
    if "NISAR" in sensors:
        source = frozen.get("nisar_source")
        if not source or not source["asset_id"]:
            fail(422, "SATELLITE_NEEDS_DATA", "Freeze the commissioned calibrated NISAR source before processing.")
        if source["asset_type"] == "image":
            nisar = ee.Image(source["asset_id"])
            sources["NISAR"] = {"dataset": source["asset_id"], "asset": ee.data.getAsset(source["asset_id"])}
        else:
            collection = ee.ImageCollection(source["asset_id"]).filterBounds(region).filterDate(start, end)
            collection, sources["NISAR"] = pin_collection(ee, collection, source["asset_id"])
            nisar = collection.median()
        hh, hv = nisar.select(source["hh_band"]), nisar.select(source["hv_band"])
        nisar = ee.Image.cat([hh.rename("NISAR_HH"), hv.rename("NISAR_HV"), hh.divide(hv).rename("NISAR_HH_HV_RATIO")]).clip(region)
        sources["NISAR"]["calibrated_band_mapping"] = source
        for name in ("NISAR_HH", "NISAR_HV", "NISAR_HH_HV_RATIO"):
            bands[name] = nisar.select(name)
    if set(names) - set(bands):
        fail(422, "FEATURE_SCHEMA_MISMATCH", "Selected model requires unavailable features or sensors.")
    expected_units = {"S1_VH": "dB", "S1_VV": "dB", "S1_VH_VV_RATIO": "linear_ratio", "NDVI": "dimensionless", "EVI": "dimensionless", "RED_EDGE": "reflectance", "GEDI_RH98": "m", "ELEVATION": "m", "SLOPE": "degree"}
    for name, unit in zip(names, schema["units"]):
        if name in expected_units and unit != expected_units[name]:
            fail(422, "FEATURE_UNIT_MISMATCH", "Feature units differ from this model's declared schema.")
    image = ee.Image.cat([bands[name] for name in names])
    provenance = {"observation_start": start, "observation_end_exclusive": end, "sensors": sources, "preprocessing": schema["preprocessing"]}
    return (image, provenance) if with_provenance else image
