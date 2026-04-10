"""
Satellite service — unified Google Earth Engine data fetcher.

Provides modular functions to fetch and process satellite imagery
from multiple GEE collections:

- **Sentinel-1** (C-band SAR — VH, VV)
- **Sentinel-2** (Optical — cloud-filtered median composite)
- **GEDI** (LiDAR canopy height — rh98)
- **SRTM** (Elevation + slope)
- **Vegetation indices** (NDVI, EVI, Red Edge)

Each function returns an ``ee.Image`` that can be combined into a
multi-band feature stack for biomass modelling.
"""

import logging
from datetime import datetime, timedelta
import hashlib
import json
from typing import Any, Dict, Optional, Tuple

import ee
import httpx

from app.database import supabase_client
from app.gee import ensure_gee_initialized

logger = logging.getLogger("terratrust.satellite")

THUMBNAIL_BUCKET = "land-documents"
THUMBNAIL_TTL_SECONDS = 7 * 24 * 60 * 60


# ---------------------------------------------------------------------------
# GEE initialisation helper
# ---------------------------------------------------------------------------
def _ensure_gee() -> None:
    """Initialise Google Earth Engine if not already done (idempotent).
    """
    ensure_gee_initialized()


def _build_ee_region(boundary_geojson: Dict[str, Any]) -> ee.Geometry:
    """Convert a GeoJSON geometry dict to an ``ee.Geometry``.

    Parameters
    ----------
    boundary_geojson : dict
        GeoJSON with ``type`` (Polygon | MultiPolygon) and ``coordinates``.

    Returns
    -------
    ee.Geometry
    """
    coords = boundary_geojson.get("coordinates", [])
    geom_type = boundary_geojson.get("type", "Polygon")
    if geom_type == "Polygon":
        return ee.Geometry.Polygon(coords)
    elif geom_type == "MultiPolygon":
        return ee.Geometry.MultiPolygon(coords)
    else:
        raise ValueError(f"Unsupported geometry type: {geom_type}")


def _default_date_range(days_back: int = 180) -> Tuple[str, str]:
    """Return (start, end) date strings for the last ``days_back`` days."""
    end = datetime.utcnow()
    start = end - timedelta(days=days_back)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Sentinel-1 (C-band SAR)
# ---------------------------------------------------------------------------
def fetch_sentinel1(
    region: ee.Geometry,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
    speckle_filter: bool = True,
) -> ee.Image:
    """Fetch a Sentinel-1 GRD median composite (VH + VV).

    Parameters
    ----------
    region : ee.Geometry
        Area of interest.
    date_start, date_end : str, optional
        ISO date strings.  Defaults to last 180 days.
    speckle_filter : bool, optional
        Apply 3×3 focal median speckle filter (default True).

    Returns
    -------
    ee.Image
        Two-band image with ``VH`` and ``VV`` bands.
    """
    _ensure_gee()

    if not date_start or not date_end:
        date_start, date_end = _default_date_range()

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

    if speckle_filter:
        s1 = s1.focal_median(3, "square", "pixels")

    logger.info("Fetched Sentinel-1 composite (%s → %s).", date_start, date_end)
    return s1


# ---------------------------------------------------------------------------
# Sentinel-2 (Optical)
# ---------------------------------------------------------------------------
def fetch_sentinel2(
    region: ee.Geometry,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
    max_cloud_pct: int = 20,
) -> ee.Image:
    """Fetch a Sentinel-2 SR Harmonized cloud-filtered median composite.

    Parameters
    ----------
    region : ee.Geometry
        Area of interest.
    date_start, date_end : str, optional
        ISO date strings.  Defaults to last 180 days.
    max_cloud_pct : int, optional
        Maximum cloud cover percentage (default 20).

    Returns
    -------
    ee.Image
        Multi-band optical image.
    """
    _ensure_gee()

    if not date_start or not date_end:
        date_start, date_end = _default_date_range()

    s2 = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(region)
        .filterDate(date_start, date_end)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", max_cloud_pct))
        .median()
    )

    logger.info(
        "Fetched Sentinel-2 composite (%s → %s, cloud≤%d%%).",
        date_start,
        date_end,
        max_cloud_pct,
    )
    return s2


# ---------------------------------------------------------------------------
# Vegetation indices
# ---------------------------------------------------------------------------
def compute_vegetation_indices(
    s2_image: ee.Image,
) -> Tuple[ee.Image, ee.Image, ee.Image]:
    """Compute NDVI, EVI, and Red Edge index from a Sentinel-2 image.

    Parameters
    ----------
    s2_image : ee.Image
        Sentinel-2 median composite (must contain B2, B4, B7, B8).

    Returns
    -------
    tuple[ee.Image, ee.Image, ee.Image]
        ``(ndvi, evi, red_edge)`` — each is a single-band image.
    """
    # NDVI = (NIR − RED) / (NIR + RED)
    ndvi = s2_image.normalizedDifference(["B8", "B4"]).rename("NDVI")

    # EVI = 2.5 × (NIR − RED) / (NIR + 6×RED − 7.5×BLUE + 1)
    evi = s2_image.expression(
        "2.5 * ((NIR - RED) / (NIR + 6*RED - 7.5*BLUE + 1))",
        {
            "NIR": s2_image.select("B8"),
            "RED": s2_image.select("B4"),
            "BLUE": s2_image.select("B2"),
        },
    ).rename("EVI")

    # Red Edge (B7 — 783 nm)
    red_edge = s2_image.select("B7").rename("RED_EDGE")

    logger.info("Computed vegetation indices: NDVI, EVI, RED_EDGE.")
    return ndvi, evi, red_edge


# ---------------------------------------------------------------------------
# GEDI canopy height
# ---------------------------------------------------------------------------
def fetch_gedi_canopy(region: ee.Geometry) -> ee.Image:
    """Fetch the GEDI Level 2A canopy height (rh98) mean image.

    GEDI (Global Ecosystem Dynamics Investigation) provides spaceborne
    LiDAR measurements of forest vertical structure.  ``rh98`` represents
    the height at the 98th percentile of returned waveform energy, which
    closely approximates canopy top height.

    Parameters
    ----------
    region : ee.Geometry
        Area of interest.

    Returns
    -------
    ee.Image
        Single-band image ``GEDI_RH98`` (metres).
    """
    _ensure_gee()

    gedi = (
        ee.ImageCollection("LARSE/GEDI/GEDI02_A_002_MONTHLY")
        .filterBounds(region)
        .select(["rh98"])
        .mean()
        .rename("GEDI_RH98")
    )

    logger.info("Fetched GEDI canopy height (rh98).")
    return gedi


# ---------------------------------------------------------------------------
# SRTM terrain
# ---------------------------------------------------------------------------
def fetch_srtm_terrain(region: ee.Geometry) -> Tuple[ee.Image, ee.Image]:
    """Fetch SRTM elevation and derived slope.

    Parameters
    ----------
    region : ee.Geometry
        Area of interest (used for context; SRTM is global).

    Returns
    -------
    tuple[ee.Image, ee.Image]
        ``(elevation, slope)`` — each a single-band image.
    """
    _ensure_gee()

    elevation = ee.Image("USGS/SRTMGL1_003").select("elevation").rename("ELEVATION")
    slope = ee.Terrain.slope(elevation).rename("SLOPE")

    logger.info("Fetched SRTM elevation + slope.")
    return elevation, slope


# ---------------------------------------------------------------------------
# Build full feature stack
# ---------------------------------------------------------------------------
def build_feature_stack(
    boundary_geojson: Dict[str, Any],
    days_back: int = 180,
    max_cloud_pct: int = 20,
    speckle_filter: bool = True,
) -> ee.Image:
    """Build a multi-band feature stack by combining all satellite layers.

    Assembles: Sentinel-1 (VH, VV), NDVI, EVI, RED_EDGE,
    GEDI_RH98, ELEVATION, SLOPE — clipped to the parcel boundary.

    Parameters
    ----------
    boundary_geojson : dict
        GeoJSON geometry of the land parcel.
    days_back : int, optional
        Number of days to look back for composites (default 180).
    max_cloud_pct : int, optional
        Max cloud cover for Sentinel-2 filtering (default 20).
    speckle_filter : bool, optional
        Apply speckle filter on Sentinel-1 (default True).

    Returns
    -------
    ee.Image
        9-band image: ``S1_VH, S1_VV, S1_VH_VV_RATIO, NDVI, EVI,
        RED_EDGE, GEDI_RH98, ELEVATION, SLOPE``, clipped to the parcel boundary.
    """
    _ensure_gee()

    region = _build_ee_region(boundary_geojson)
    date_start, date_end = _default_date_range(days_back)

    # Fetch individual layers
    s1 = fetch_sentinel1(region, date_start, date_end, speckle_filter)
    s1_vh = s1.select("VH").rename("S1_VH")
    s1_vv = s1.select("VV").rename("S1_VV")
    s1_ratio = s1_vh.divide(s1_vv).rename("S1_VH_VV_RATIO")
    s2 = fetch_sentinel2(region, date_start, date_end, max_cloud_pct)
    ndvi, evi, red_edge = compute_vegetation_indices(s2)
    gedi = fetch_gedi_canopy(region)
    elevation, slope = fetch_srtm_terrain(region)

    # Stack all bands
    feature_stack = ee.Image.cat(
        [s1_vh, s1_vv, s1_ratio, ndvi, evi, red_edge, gedi, elevation, slope]
    ).clip(region)

    logger.info(
        "Built 9-band feature stack: S1_VH, S1_VV, S1_VH_VV_RATIO, NDVI, "
        "EVI, RED_EDGE, GEDI_RH98, ELEVATION, SLOPE."
    )
    return feature_stack


def generate_true_color_thumbnail_url(
    boundary_geojson: Dict[str, Any],
    dimensions: int = 1024,
    max_cloud_pct: int = 20,
) -> str:
    """Return a signed Supabase URL for a persisted Sentinel-2 true-colour PNG."""
    _ensure_gee()

    region = _build_ee_region(boundary_geojson)
    date_start, date_end = _default_date_range(365)
    rgb = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(region)
        .filterDate(date_start, date_end)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", max_cloud_pct))
        .median()
        .select(["B4", "B3", "B2"])
        .clip(region)
    )

    raw_thumbnail_url = rgb.getThumbURL(
        {
            "region": region,
            "dimensions": dimensions,
            "format": "png",
            "min": 0,
            "max": 3000,
        }
    )

    thumbnail_key = hashlib.sha256(
        json.dumps(boundary_geojson, sort_keys=True).encode("utf-8")
    ).hexdigest()
    object_path = f"thumbnails/{thumbnail_key}-{dimensions}.png"

    response = httpx.get(raw_thumbnail_url, timeout=60.0)
    response.raise_for_status()

    try:
        supabase_client.storage.from_(THUMBNAIL_BUCKET).upload(
            object_path,
            response.content,
        )
    except Exception as exc:
        if "exists" not in str(exc).lower() and "duplicate" not in str(exc).lower():
            raise

    signed_response = supabase_client.storage.from_(THUMBNAIL_BUCKET).create_signed_url(
        object_path,
        THUMBNAIL_TTL_SECONDS,
    )
    if isinstance(signed_response, dict):
        signed_url = (
            signed_response.get("signedURL")
            or signed_response.get("signedUrl")
            or signed_response.get("signed_url")
        )
        if signed_url:
            return signed_url

    raise RuntimeError("Supabase did not return a signed URL for the generated thumbnail.")


# ---------------------------------------------------------------------------
# Region statistics
# ---------------------------------------------------------------------------
def get_satellite_stats(
    boundary_geojson: Dict[str, Any],
    days_back: int = 180,
) -> Dict[str, Any]:
    """Compute mean satellite feature statistics for a land parcel.

    Useful for quick diagnostic checks and audit metadata without
    running the full fusion pipeline.

    Parameters
    ----------
    boundary_geojson : dict
        GeoJSON geometry of the land parcel.
    days_back : int, optional
        Number of days to look back (default 180).

    Returns
    -------
    dict
        Mean values aligned to the documented audit metadata fields.
    """
    _ensure_gee()

    region = _build_ee_region(boundary_geojson)
    feature_stack = build_feature_stack(boundary_geojson, days_back)

    stats = (
        feature_stack.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=region,
            scale=10,
            maxPixels=1e8,
        )
        .getInfo()
    )

    result = {
        "s1_vh_mean_db": stats.get("S1_VH", 0),
        "s1_vv_mean_db": stats.get("S1_VV", 0),
        "s1_vh_vv_ratio_mean": stats.get("S1_VH_VV_RATIO", 0),
        "s2_ndvi_mean": stats.get("NDVI", 0),
        "s2_evi_mean": stats.get("EVI", 0),
        "s2_red_edge_mean": stats.get("RED_EDGE", 0),
        "gedi_height_mean": stats.get("GEDI_RH98", 0),
        "srtm_elevation_mean": stats.get("ELEVATION", 0),
        "srtm_slope_mean": stats.get("SLOPE", 0),
    }

    logger.info(
        "Satellite stats for region: NDVI=%.3f, EVI=%.3f, GEDI=%.1f m, "
        "elevation=%.0f m, VH=%.1f dB",
        result["s2_ndvi_mean"] or 0,
        result["s2_evi_mean"] or 0,
        result["gedi_height_mean"] or 0,
        result["srtm_elevation_mean"] or 0,
        result["s1_vh_mean_db"] or 0,
    )
    return result
