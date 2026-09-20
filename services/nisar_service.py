"""NISAR service for ASF granule search and download."""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

try:
    import asf_search as asf
except ImportError:  # pragma: no cover - depends on optional local install
    asf = None

from app.config import settings

logger = logging.getLogger("terratrust.nisar")


def _require_asf_search() -> Any:
    """Return the ASF client module or fail only when NISAR support is used."""
    if asf is None:
        raise RuntimeError(
            "asf_search is not installed. Install backend requirements to enable NISAR support."
        )
    return asf


def build_nisar_feature_image(
    region: Any,
    date_start: str,
    date_end: str,
) -> Any:
    """Build the calibrated NISAR HH/HV/ratio feature image from a GEE asset.

    This is the **production** path used by :func:`fusion_engine.run_fusion`
    when ``NISAR_PRODUCTION_READY=True``.  The ASF service can download source
    products, but production fusion needs calibrated, georeferenced raster bands
    published as an Earth Engine image or image collection, referenced by
    ``NISAR_GEE_ASSET_ID``.

    Parameters
    ----------
    region:
        An ``ee.Geometry`` that clips the returned image.
    date_start, date_end:
        ISO-8601 date strings (``YYYY-MM-DD``) for the composite window.

    Returns
    -------
    ee.Image
        Three-band image with bands ``NISAR_HH``, ``NISAR_HV``,
        ``NISAR_HH_HV_RATIO``, all clipped to *region*.

    Raises
    ------
    RuntimeError
        If ``NISAR_GEE_ASSET_ID`` is not configured or
        ``earthengine-api`` is not installed.
    """
    asset_id = settings.NISAR_GEE_ASSET_ID.strip()
    if not asset_id:
        raise RuntimeError(
            "NISAR_GEE_ASSET_ID must be configured to point to "
            "a calibrated Earth Engine asset with HH and HV bands."
        )

    try:
        import ee
    except ImportError as exc:  # pragma: no cover - earthengine-api is a runtime dependency
        raise RuntimeError("earthengine-api is required for NISAR fusion.") from exc

    if settings.NISAR_GEE_ASSET_TYPE == "image":
        nisar_source = ee.Image(asset_id)
    else:
        nisar_source = (
            ee.ImageCollection(asset_id)
            .filterBounds(region)
            .filterDate(date_start, date_end)
            .median()
        )

    hh = nisar_source.select(settings.NISAR_HH_BAND).rename("NISAR_HH")
    hv = nisar_source.select(settings.NISAR_HV_BAND).rename("NISAR_HV")
    ratio = hh.divide(hv).rename("NISAR_HH_HV_RATIO")
    return ee.Image.cat([hh, hv, ratio]).clip(region)


# ---------------------------------------------------------------------------
# Authentication helper
# ---------------------------------------------------------------------------
def _get_earthdata_session() -> asf.ASFSession:
    """Return an authenticated ASF session using NASA Earthdata credentials.

    Raises
    ------
    ValueError
        If NASA Earthdata credentials are not configured.
    """
    username = settings.NASA_EARTHDATA_USERNAME
    password = settings.NASA_EARTHDATA_PASSWORD

    if not username or not password:
        raise ValueError(
            "NASA Earthdata credentials are not configured. "
            "Set NASA_EARTHDATA_USERNAME and NASA_EARTHDATA_PASSWORD in .env"
        )

    asf_module = _require_asf_search()
    session = asf_module.ASFSession()
    session.auth_with_creds(username, password)
    logger.info("Authenticated with NASA Earthdata as '%s'.", username)
    return session


# ---------------------------------------------------------------------------
# Search NISAR granules
# ---------------------------------------------------------------------------
def search_nisar_granules(
    boundary_geojson: Dict[str, Any],
    days_back: int = 180,
    max_results: int = 10,
) -> List[Dict[str, Any]]:
    """Search for NISAR L-band SAR granules intersecting a land boundary.

    Parameters
    ----------
    boundary_geojson : dict
        GeoJSON geometry (``Polygon`` or ``MultiPolygon``) of the
        land parcel.
    days_back : int, optional
        Number of days to look back from today (default 180).
    max_results : int, optional
        Maximum number of granules to return (default 10).

    Returns
    -------
    list[dict]
        Each dict contains: ``granule_name``, ``download_url``,
        ``acquisition_date``, ``platform``, ``beam_mode``,
        ``polarisation``, ``file_size_mb``, ``browse_url``.
    """
    # Build WKT from GeoJSON coordinates
    coords = boundary_geojson.get("coordinates", [])
    geom_type = boundary_geojson.get("type", "Polygon")

    if geom_type == "Polygon":
        ring = coords[0]  # outer ring
        wkt = "POLYGON((" + ",".join(f"{lng} {lat}" for lng, lat in ring) + "))"
    elif geom_type == "MultiPolygon":
        parts = []
        for polygon in coords:
            ring = polygon[0]
            parts.append("((" + ",".join(f"{lng} {lat}" for lng, lat in ring) + "))")
        wkt = "MULTIPOLYGON(" + ",".join(parts) + ")"
    else:
        raise ValueError(f"Unsupported geometry type: {geom_type}")

    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=days_back)

    logger.info(
        "Searching NISAR granules: %s → %s, max_results=%d",
        start_date.strftime("%Y-%m-%d"),
        end_date.strftime("%Y-%m-%d"),
        max_results,
    )

    # ASF search — NISAR uses platform "NISAR"
    asf_module = _require_asf_search()
    results: List[Any] = []

    try:
        nisar_results = asf_module.search(
            platform=[asf_module.PLATFORM.NISAR],
            intersectsWith=wkt,
            start=start_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
            end=end_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
            maxResults=max_results,
        )
        results.extend(nisar_results)
        logger.info("Found %d NISAR granules.", len(nisar_results))
    except Exception as exc:
        logger.warning("NISAR search failed or returned no data: %s", exc)

    # Format results
    granules: List[Dict[str, Any]] = []
    for product in results:
        props = product.properties
        granules.append(
            {
                "granule_name": props.get("sceneName", props.get("fileID", "")),
                "download_url": props.get("url", ""),
                "acquisition_date": props.get("startTime", ""),
                "platform": props.get("platform", ""),
                "beam_mode": props.get("beamMode", ""),
                "polarisation": props.get("polarization", ""),
                "file_size_mb": round(
                    float(props.get("bytes", 0)) / (1024 * 1024), 2
                ),
                "browse_url": props.get("browse", ""),
            }
        )

    logger.info("Returning %d granule(s) for the requested boundary.", len(granules))
    return granules


# ---------------------------------------------------------------------------
# Download a single NISAR scene
# ---------------------------------------------------------------------------
def download_nisar_scene(
    download_url: str,
    output_dir: Optional[str] = None,
) -> str:
    """Download a NISAR granule to a local directory.

    Parameters
    ----------
    download_url : str
        Direct download URL from ASF (as returned by ``search_nisar_granules``).
    output_dir : str, optional
        Directory to save the file.  Defaults to a temporary directory.

    Returns
    -------
    str
        Absolute path to the downloaded file.

    Raises
    ------
    RuntimeError
        If the download fails.
    """
    if not output_dir:
        output_dir = tempfile.mkdtemp(prefix="nisar_")

    session = _get_earthdata_session()

    logger.info("Downloading NISAR scene to '%s'…", output_dir)

    try:
        # asf_search download helper
        asf_module = _require_asf_search()
        asf_module.download_url(
            url=download_url,
            path=output_dir,
            session=session,
        )
    except Exception as exc:
        raise RuntimeError(f"NISAR download failed: {exc}") from exc

    # Find the downloaded file
    downloaded_files = os.listdir(output_dir)
    if not downloaded_files:
        raise RuntimeError("Download completed but no file found in output directory.")

    file_path = os.path.join(output_dir, downloaded_files[0])
    logger.info("Downloaded NISAR scene: %s (%.1f MB)", file_path, os.path.getsize(file_path) / 1e6)
    return file_path


# ---------------------------------------------------------------------------
# Extract L-band backscatter statistics
# ---------------------------------------------------------------------------
def _extract_nisar_gee_backscatter(
    boundary_geojson: Dict[str, Any],
    date_start: str,
    date_end: str,
) -> Dict[str, Any]:
    """Sample mean HH/HV backscatter for a parcel from the configured GEE asset.

    This is the production implementation used when ``NISAR_PRODUCTION_READY=True``
    and ``NISAR_GEE_ASSET_ID`` points to a calibrated Earth Engine asset.

    Returns a stats dict with keys:
    ``available``, ``hh_mean_db``, ``hv_mean_db``, ``hh_hv_ratio``.
    """
    try:
        import ee
        from app.gee import ensure_gee_initialized
        ensure_gee_initialized()
    except Exception as exc:
        logger.warning("GEE not available for NISAR backscatter extraction: %s", exc)
        return {"available": False, "hh_mean_db": None, "hv_mean_db": None, "hh_hv_ratio": None}

    geom_type = boundary_geojson.get("type", "Polygon")
    coords = boundary_geojson.get("coordinates", [])
    try:
        if geom_type == "Polygon":
            region = ee.Geometry.Polygon(coords)
        elif geom_type == "MultiPolygon":
            region = ee.Geometry.MultiPolygon(coords)
        else:
            raise ValueError(f"Unsupported geometry type: {geom_type}")
    except Exception as exc:
        logger.warning("Could not build GEE geometry for NISAR backscatter: %s", exc)
        return {"available": False, "hh_mean_db": None, "hv_mean_db": None, "hh_hv_ratio": None}

    try:
        nisar_img = build_nisar_feature_image(region, date_start, date_end)
        stats = nisar_img.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=region,
            scale=20,
            maxPixels=1e8,
        ).getInfo()
    except Exception as exc:
        logger.warning("NISAR GEE reduceRegion failed: %s", exc)
        return {"available": False, "hh_mean_db": None, "hv_mean_db": None, "hh_hv_ratio": None}

    hh_mean = stats.get("NISAR_HH")
    hv_mean = stats.get("NISAR_HV")
    ratio_mean = stats.get("NISAR_HH_HV_RATIO")

    if hh_mean is None and hv_mean is None:
        logger.info("NISAR GEE asset returned no data for this region.")
        return {"available": False, "hh_mean_db": None, "hv_mean_db": None, "hh_hv_ratio": None}

    logger.info(
        "NISAR GEE backscatter: HH=%.2f dB, HV=%.2f dB, ratio=%.4f",
        hh_mean or 0,
        hv_mean or 0,
        ratio_mean or 0,
    )
    return {
        "available": True,
        "hh_mean_db": round(float(hh_mean), 4) if hh_mean is not None else None,
        "hv_mean_db": round(float(hv_mean), 4) if hv_mean is not None else None,
        "hh_hv_ratio": round(float(ratio_mean), 6) if ratio_mean is not None else None,
    }


def extract_nisar_backscatter(
    boundary_geojson: Dict[str, Any],
    days_back: int = 365,
) -> Dict[str, Any]:
    """Extract L-band HH/HV backscatter statistics for a land parcel.

    When ``NISAR_PRODUCTION_READY=True`` and ``NISAR_GEE_ASSET_ID`` is set,
    this function samples mean HH and HV backscatter directly from the
    calibrated Earth Engine asset.  This is the **only production-quality**
    path that returns real numeric values.

    When the GEE asset is not configured (``NISAR_PRODUCTION_READY=False``),
    the function falls back to the ASF availability stub: it searches for
    recent NISAR granules via the ASF API to confirm data exists for the
    region, but returns ``None`` for the numeric fields because the raw
    granules have not been calibrated and ingested into Earth Engine yet.
    In this mode ``available=True`` only means a raw granule was found, not
    that the numeric backscatter values are usable.

    Parameters
    ----------
    boundary_geojson : dict
        GeoJSON geometry of the land parcel.
    days_back : int, optional
        Look-back window for ASF granule search (default 365).  Only used
        in the fallback stub path.

    Returns
    -------
    dict
        Keys: ``available``, ``platform``, ``acquisition_date``,
        ``polarisation``, ``hh_mean_db``, ``hv_mean_db``,
        ``hh_hv_ratio``, ``granule_name``.
    """
    if settings.NISAR_GEE_ASSET_ID.strip():
        # Production path — sample from the calibrated GEE asset.
        audit_year = datetime.now(timezone.utc).year
        date_start = f"{audit_year - 1}-01-01"
        date_end = f"{audit_year}-12-31"
        gee_stats = _extract_nisar_gee_backscatter(boundary_geojson, date_start, date_end)
        if gee_stats.get("available"):
            return {
                "available": True,
                "platform": "NISAR",
                "acquisition_date": date_end,
                "polarisation": "HH+HV",
                "hh_mean_db": gee_stats["hh_mean_db"],
                "hv_mean_db": gee_stats["hv_mean_db"],
                "hh_hv_ratio": gee_stats["hh_hv_ratio"],
                "granule_name": settings.NISAR_GEE_ASSET_ID.strip(),
            }

    # --- Fallback: ASF granule availability check ---------------------------
    # If GEE asset returned no data or is not configured, check ASF for granule existence.
    logger.info(
        "Calibrated NISAR GEE data unavailable; running ASF granule availability check."
    )
    granules = search_nisar_granules(
        boundary_geojson=boundary_geojson,
        days_back=days_back,
        max_results=5,
    )

    if not granules:
        logger.info("No NISAR L-band data available for this region.")
        return {
            "available": False,
            "platform": None,
            "acquisition_date": None,
            "polarisation": None,
            "hh_mean_db": None,
            "hv_mean_db": None,
            "hh_hv_ratio": None,
            "granule_name": None,
        }

    latest = granules[0]
    logger.info(
        "L-band granule found (availability only — not yet ingested to GEE): %s (%s, %s)",
        latest["granule_name"],
        latest["platform"],
        latest["acquisition_date"],
    )
    return {
        "available": True,
        "platform": latest["platform"],
        "acquisition_date": latest["acquisition_date"],
        "polarisation": latest["polarisation"],
        # Numeric fields are None: raw granule requires GEE calibration/ingestion.
        "hh_mean_db": None,
        "hv_mean_db": None,
        "hh_hv_ratio": None,
        "granule_name": latest["granule_name"],
    }
