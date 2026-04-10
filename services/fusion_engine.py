"""Fusion engine implementing the documented satellite + field workflow."""

import logging
from typing import Any, Dict, List, Optional

import ee

from app.database import supabase_client
from app.gee import ensure_gee_initialized

logger = logging.getLogger("terratrust.fusion")

# ---------------------------------------------------------------------------
# Species wood density (g/cm³ → t/m³ equivalent)
# ---------------------------------------------------------------------------
SPECIES_WOOD_DENSITY: Dict[str, float] = {
    "Teak": 0.60,
    "Eucalyptus": 0.55,
    "Neem": 0.56,
    "Mango": 0.54,
    "Bamboo": 0.70,
    "Pongamia": 0.67,
    "Subabul": 0.56,
    "Casuarina": 0.69,
    "Indian Rosewood": 0.75,
    "Drumstick": 0.39,
    "Amla": 0.74,
}

SPECIES_ALIASES: Dict[str, str] = {
    "dalbergia sissoo": "Indian Rosewood",
    "shisham": "Indian Rosewood",
    "north indian rosewood": "Indian Rosewood",
}


def _ensure_gee() -> None:
    """Initialise GEE if not already done."""
    ensure_gee_initialized()


def normalise_species_name(species: str) -> str:
    """Return the canonical approved species label for the submitted name."""
    candidate = " ".join(species.split()).casefold()
    if not candidate:
        raise ValueError("Species name cannot be empty.")

    for approved_species in SPECIES_WOOD_DENSITY:
        if approved_species.casefold() == candidate:
            return approved_species

    alias_match = SPECIES_ALIASES.get(candidate)
    if alias_match:
        return alias_match

    raise ValueError(
        "Unsupported tree species. Allowed values are: "
        + ", ".join(sorted(SPECIES_WOOD_DENSITY))
        + "."
    )


def wood_density_for_species(species: str) -> float:
    """Return the documented wood density for a canonical approved species."""
    canonical_species = normalise_species_name(species)
    return SPECIES_WOOD_DENSITY[canonical_species]


def _extract_scan_gps(scan: Dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    """Return ``(lat, lng)`` from either nested or flattened scan payloads."""
    gps = scan.get("gps")
    if isinstance(gps, dict):
        lat = gps.get("lat")
        lng = gps.get("lng")
    else:
        lat = scan.get("lat")
        lng = scan.get("lng")

    try:
        return float(lat), float(lng)
    except (TypeError, ValueError):
        return None, None


def _sample_gedi_height(gedi_image: ee.Image, lat: float, lng: float) -> Optional[float]:
    """Sample GEDI height at a tree location, returning ``None`` when absent."""
    try:
        feature = gedi_image.sample(
            region=ee.Geometry.Point([lng, lat]),
            scale=25,
        ).first()
        if feature is None:
            return None

        sample_info = feature.getInfo() or {}
        properties = sample_info.get("properties", {})
        value = properties.get("GEDI_RH98")
        if value is None:
            value = properties.get("GEDI_HEIGHT")

        if value is None:
            return None

        height = float(value)
        return height if height > 0 else None
    except Exception:
        return None


def _sample_zone_gedi_height(gedi_image: ee.Image, zone: Dict[str, Any]) -> Optional[float]:
    """Resolve the nearest valid GEDI sample inside a scan-required zone."""
    centre = zone.get("centre_gps") or {}
    lat = centre.get("lat")
    lng = centre.get("lng")
    radius_metres = zone.get("radius_metres")

    try:
        lat = float(lat)
        lng = float(lng)
        radius_metres = float(radius_metres)
    except (TypeError, ValueError):
        return None

    try:
        feature = gedi_image.sample(
            region=ee.Geometry.Point([lng, lat]).buffer(radius_metres),
            scale=25,
            numPixels=1,
            geometries=False,
        ).first()
        if feature is None:
            return None

        sample_info = feature.getInfo() or {}
        properties = sample_info.get("properties", {})
        value = properties.get("GEDI_RH98")
        if value is None:
            value = properties.get("GEDI_HEIGHT")

        if value is None:
            return None

        height = float(value)
        return height if height > 0 else None
    except Exception:
        return None


def _image_has_valid_pixels(image: ee.Image, region: ee.Geometry, scale: int) -> bool:
    """Return whether an Earth Engine image has any unmasked pixels in a region."""
    stats = image.reduceRegion(
        reducer=ee.Reducer.count(),
        geometry=region,
        scale=scale,
        maxPixels=1e8,
    ).getInfo()
    return any(((value or 0) > 0) for value in (stats or {}).values())


# ---------------------------------------------------------------------------
# Main fusion function
# ---------------------------------------------------------------------------
def run_fusion(
    audit_id: str,
    land_id: str,
    tree_scans: List[Dict[str, Any]],
    land_boundary_geojson: Dict[str, Any],
    audit_year: int,
    sampling_zones: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run the multi-source data fusion and biomass estimation.

    Parameters
    ----------
    audit_id : str
        UUID of the current audit.
    land_id : str
        UUID of the land parcel.
    tree_scans : list[dict]
        List of AR tree scan records with keys:
        species, dbh_cm, height_m (optional), gps:{lat,lng},
        gedi_height_m (optional).
    land_boundary_geojson : dict
        GeoJSON Polygon / MultiPolygon of the parcel.
    audit_year : int
        Calendar year of the audit.

    Returns
    -------
    dict
        ``{total_biomass_tonnes, satellite_features, training_points_count}``
    """
    _ensure_gee()

    # --- EE geometry -------------------------------------------------------
    coords = land_boundary_geojson.get("coordinates", [])
    geom_type = land_boundary_geojson.get("type", "Polygon")
    if geom_type == "Polygon":
        region = ee.Geometry.Polygon(coords)
    elif geom_type == "MultiPolygon":
        region = ee.Geometry.MultiPolygon(coords)
    else:
        raise ValueError(f"Unsupported geometry type: {geom_type}")

    date_start = f"{audit_year}-01-01"
    date_end = f"{audit_year}-12-31"

    # -----------------------------------------------------------------------
    # 1. Sentinel-1 (VH, VV) — speckle filtered
    # -----------------------------------------------------------------------
    s1 = (
        ee.ImageCollection("COPERNICUS/S1_GRD")
        .filterBounds(region)
        .filterDate(date_start, date_end)
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
        .select(["VH", "VV"])
        .median()
    )
    # Speckle filter — focal median 3×3
    s1_filtered = s1.focal_median(3, "square", "pixels")
    s1_vh = s1_filtered.select("VH").rename("S1_VH")
    s1_vv = s1_filtered.select("VV").rename("S1_VV")
    s1_ratio = s1_vh.divide(s1_vv).rename("S1_VH_VV_RATIO")

    # -----------------------------------------------------------------------
    # 2. Sentinel-2 optical — cloud-filtered median composite
    # -----------------------------------------------------------------------
    s2 = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(region)
        .filterDate(date_start, date_end)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 20))
        .median()
    )

    # -----------------------------------------------------------------------
    # 3. Vegetation indices
    # -----------------------------------------------------------------------
    ndvi = s2.normalizedDifference(["B8", "B4"]).rename("NDVI")

    evi = s2.expression(
        "2.5 * ((NIR - RED) / (NIR + 6*RED - 7.5*BLUE + 1))",
        {
            "NIR": s2.select("B8"),
            "RED": s2.select("B4"),
            "BLUE": s2.select("B2"),
        },
    ).rename("EVI")

    red_edge = s2.select("B7").rename("RED_EDGE")

    # -----------------------------------------------------------------------
    # 4. GEDI canopy height
    # -----------------------------------------------------------------------
    raw_gedi = (
        ee.ImageCollection("LARSE/GEDI/GEDI02_A_002_MONTHLY")
        .filterBounds(region)
        .select(["rh98"])
        .mean()
        .rename("GEDI_RH98")
    )
    zone_lookup: Dict[str, Dict[str, Any]] = {}
    if sampling_zones:
        zone_lookup = {
            str(zone.get("id") or zone.get("zone_id")): zone
            for zone in sampling_zones
            if zone.get("id") or zone.get("zone_id")
        }

    if zone_lookup:
        gedi_feature_enabled = any(bool(zone.get("gedi_available")) for zone in zone_lookup.values())
    else:
        gedi_feature_enabled = _image_has_valid_pixels(raw_gedi, region, scale=25)

    gedi = raw_gedi.unmask(0) if gedi_feature_enabled else None

    # -----------------------------------------------------------------------
    # 5. SRTM elevation & slope
    # -----------------------------------------------------------------------
    srtm = ee.Image("USGS/SRTMGL1_003").select("elevation").rename("ELEVATION")
    slope = ee.Terrain.slope(srtm).rename("SLOPE")

    # -----------------------------------------------------------------------
    # 6. Stack all bands into a single feature image
    # -----------------------------------------------------------------------
    feature_bands = [s1_vh, s1_vv, s1_ratio, ndvi, evi, red_edge]
    band_names = ["S1_VH", "S1_VV", "S1_VH_VV_RATIO", "NDVI", "EVI", "RED_EDGE"]

    if gedi is not None:
        feature_bands.append(gedi)
        band_names.append("GEDI_RH98")

    feature_bands.extend([srtm, slope])
    band_names.extend(["ELEVATION", "SLOPE"])

    feature_stack = ee.Image.cat(feature_bands).clip(region)

    # -----------------------------------------------------------------------
    # 7. Compute per-tree AGB using Chave allometric equation
    # -----------------------------------------------------------------------
    training_points: List[ee.Feature] = []
    tree_measurements: List[Dict[str, Any]] = []
    skipped_scans = 0
    zone_height_cache: Dict[str, Optional[float]] = {}

    for scan in tree_scans:
        species = normalise_species_name(scan.get("species", ""))
        dbh_cm = float(scan["dbh_cm"])
        lat, lng = _extract_scan_gps(scan)
        if lat is None or lng is None:
            skipped_scans += 1
            logger.warning(
                "Skipping tree scan %s because GPS coordinates are missing.",
                scan.get("id"),
            )
            continue

        zone_id = str(scan.get("zone_id") or "")
        zone = zone_lookup.get(zone_id)

        gedi_height_m = scan.get("gedi_height_m")
        if zone and zone.get("gedi_available"):
            if zone_id not in zone_height_cache:
                zone_height_cache[zone_id] = _sample_zone_gedi_height(raw_gedi, zone)
            gedi_height_m = zone_height_cache.get(zone_id)
        elif gedi_height_m is None and not zone_lookup and gedi_feature_enabled:
            gedi_height_m = _sample_gedi_height(raw_gedi, lat, lng)

        height_m = gedi_height_m if gedi_height_m is not None else scan.get("height_m")
        if height_m is None or float(height_m) <= 0:
            skipped_scans += 1
            logger.warning(
                "Skipping tree scan %s because neither GEDI nor AR fallback height is available.",
                scan.get("id"),
            )
            continue

        height_source = "GEDI" if gedi_height_m else "AR_FALLBACK"
        wood_density = wood_density_for_species(species)

        # Chave et al. (2014) pan-tropical equation
        agb_kg = 0.0673 * (wood_density * (dbh_cm**2) * height_m) ** 0.976
        agb_t_ha = (agb_kg / 1000) / 0.01  # per-tree → tonnes per hectare (0.01 ha plot)

        point = ee.Feature(
            ee.Geometry.Point([lng, lat]),
            {"AGB_THA": agb_t_ha},
        )
        training_points.append(point)
        tree_measurements.append(
            {
                "id": scan.get("id"),
                "gedi_height_m": gedi_height_m,
                "height_source": height_source,
                "agb_kg": round(agb_kg, 4),
            }
        )

    if len(training_points) < 9:
        raise ValueError(
            "Minimum 9 tree samples with valid GPS and height data are required for fusion. "
            f"Received {len(training_points)} valid sample(s); skipped {skipped_scans}."
        )

    # -----------------------------------------------------------------------
    # 8. Train XGBoost regressor on GEE
    # -----------------------------------------------------------------------
    training_fc = ee.FeatureCollection(training_points)

    training_data = feature_stack.sampleRegions(
        collection=training_fc,
        properties=["AGB_THA"],
        scale=10,
    ).filter(ee.Filter.notNull(band_names + ["AGB_THA"]))

    classifier = (
        ee.Classifier.smileGradientTreeBoost(
            numberOfTrees=100,
            shrinkage=0.05,
            samplingRate=0.7,
        )
        .setOutputMode("REGRESSION")
        .train(
            features=training_data,
            classProperty="AGB_THA",
            inputProperties=band_names,
        )
    )

    # -----------------------------------------------------------------------
    # 9. Classify entire parcel → pixel-level biomass map
    # -----------------------------------------------------------------------
    biomass_map = feature_stack.classify(classifier).rename("BIOMASS_THA")

    # -----------------------------------------------------------------------
    # 10. Sum all pixels to get total biomass (tonnes)
    # -----------------------------------------------------------------------
    # Each Sentinel-2 pixel at 10 m is 0.01 ha
    pixel_area_ha = 0.01
    biomass_sum = (
        biomass_map.multiply(pixel_area_ha)
        .reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=region,
            scale=10,
            maxPixels=1e9,
        )
        .getInfo()
    )

    total_biomass = biomass_sum.get("BIOMASS_THA", 0) or 0

    # Satellite feature statistics (for audit metadata)
    sat_stats = feature_stack.reduceRegion(
        reducer=ee.Reducer.mean(),
        geometry=region,
        scale=10,
        maxPixels=1e8,
    ).getInfo()

    logger.info(
        "Fusion complete for audit %s: total_biomass=%.2f tonnes, %d training points",
        audit_id,
        total_biomass,
        len(training_points),
    )

    return {
        "total_biomass_tonnes": round(total_biomass, 4),
        "training_points_count": len(training_points),
        "tree_measurements": tree_measurements,
        "satellite_features": {
            "s1_vh_mean_db": sat_stats.get("S1_VH", 0),
            "s1_vv_mean_db": sat_stats.get("S1_VV", 0),
            "s1_vh_vv_ratio_mean": sat_stats.get("S1_VH_VV_RATIO", 0),
            "s2_ndvi_mean": sat_stats.get("NDVI", 0),
            "s2_evi_mean": sat_stats.get("EVI", 0),
            "s2_red_edge_mean": sat_stats.get("RED_EDGE", 0),
            "gedi_height_mean": sat_stats.get("GEDI_RH98", 0),
            "srtm_elevation_mean": sat_stats.get("ELEVATION", 0),
            "srtm_slope_mean": sat_stats.get("SLOPE", 0),
            "nisar_used": False,
            "features_count": len(band_names),
            "processing_method": (
                "S1_S2_GEDI_SRTM_XGBoost_v3.1"
                if gedi_feature_enabled
                else "S1_S2_SRTM_XGBoost_v3.1"
            ),
        },
    }


# ---------------------------------------------------------------------------
# Credit calculation
# ---------------------------------------------------------------------------
def calculate_credits(
    total_biomass_tonnes: float,
    land_id: str,
    audit_year: int,
) -> Dict[str, Any]:
    """Calculate carbon credits from biomass change.

    Parameters
    ----------
    total_biomass_tonnes : float
        Current-year total above-ground biomass.
    land_id : str
        UUID of the land parcel.
    audit_year : int
        Calendar year of the current audit.

    Returns
    -------
    dict
        ``{credits_issued, delta_biomass, carbon_tonnes,
           co2_equivalent, previous_biomass, current_biomass}``
    """
    # --- Look up previous year's biomass -----------------------------------
    previous_biomass: float = 0.0
    previous_audit_found = False
    try:
        prev_resp = (
            supabase_client.table("carbon_audits")
            .select("total_biomass_tonnes, status")
            .eq("land_id", land_id)
            .lt("audit_year", audit_year)
            .order("audit_year", desc=True)
            .order("created_at", desc=True)
            .limit(10)
            .execute()
        )
        previous_audit = next(
            (
                row
                for row in (prev_resp.data or [])
                if row.get("status") in {"MINTED", "COMPLETE_NO_CREDITS"}
            ),
            None,
        )
        if previous_audit:
            previous_audit_found = True
            previous_biomass = float(previous_audit.get("total_biomass_tonnes", 0))
    except Exception as exc:
        logger.warning("Could not fetch previous biomass for land %s: %s", land_id, exc)

    if not previous_audit_found:
        return {
            "credits_issued": 0,
            "delta_biomass": 0,
            "carbon_tonnes": 0,
            "co2_equivalent": 0,
            "reason": "Baseline year established; future growth earns credits.",
            "prev_year_biomass": 0,
            "previous_biomass": 0,
            "current_biomass": total_biomass_tonnes,
        }

    delta = total_biomass_tonnes - previous_biomass

    if delta <= 0:
        no_growth_reason = "No biomass growth detected compared to the latest prior successful audit"
        return {
            "credits_issued": 0,
            "delta_biomass": round(delta, 4),
            "carbon_tonnes": 0,
            "co2_equivalent": 0,
            "reason": no_growth_reason,
            "prev_year_biomass": previous_biomass,
            "previous_biomass": previous_biomass,
            "current_biomass": total_biomass_tonnes,
        }

    # IPCC conversion factors
    carbon_tonnes = delta * 0.47
    co2_equivalent = carbon_tonnes * 3.667
    credits_issued = co2_equivalent

    logger.info(
        "Credit calculation: delta=%.2f t, carbon=%.2f t, CO2e=%.2f t -> %.4f credits",
        delta,
        carbon_tonnes,
        co2_equivalent,
        credits_issued,
    )

    return {
        "credits_issued": round(credits_issued, 4),
        "delta_biomass": round(delta, 4),
        "carbon_tonnes": round(carbon_tonnes, 4),
        "co2_equivalent": round(co2_equivalent, 4),
        "reason": "Credits issued from year-over-year biomass growth.",
        "prev_year_biomass": previous_biomass,
        "previous_biomass": previous_biomass,
        "current_biomass": total_biomass_tonnes,
    }
