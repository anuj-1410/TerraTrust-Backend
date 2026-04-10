"""NISAR service for ASF granule search and download."""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timedelta
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

    end_date = datetime.utcnow()
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
def extract_nisar_backscatter(
    boundary_geojson: Dict[str, Any],
    days_back: int = 365,
) -> Dict[str, Any]:
    """Extract L-band HH/HV backscatter statistics for biomass estimation.

    This function searches for the most recent NISAR
    L-band scene covering the parcel and computes summary statistics
    useful for the fusion engine's biomass model.

    L-band SAR penetrates canopy and is strongly correlated with
    above-ground biomass (AGB) up to ~150 t/ha, complementing the
    C-band Sentinel-1 data used in the core fusion pipeline.

    Parameters
    ----------
    boundary_geojson : dict
        GeoJSON geometry of the land parcel.
    days_back : int, optional
        Number of days to search back (default 365).

    Returns
    -------
    dict
        ``{available, platform, acquisition_date, polarisation,
          hh_mean_db, hv_mean_db, hh_hv_ratio, granule_name}``

        If no data is available, ``available`` is ``False`` and all
        numeric fields are ``None``.
    """
    # Search for granules
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

    # Use the most recent granule
    latest = granules[0]

    # For actual backscatter extraction, the granule would need to be
    # downloaded and processed with GDAL/rasterio.  Here we provide
    # the metadata indicating that L-band data IS available so the
    # fusion engine can incorporate it when full processing is added.
    #
    # Typical L-band backscatter ranges for Indian forests:
    #   HH: -8 to -4 dB   (higher = more biomass)
    #   HV: -18 to -10 dB (most sensitive to AGB)

    logger.info(
        "L-band granule found: %s (%s, %s)",
        latest["granule_name"],
        latest["platform"],
        latest["acquisition_date"],
    )

    return {
        "available": True,
        "platform": latest["platform"],
        "acquisition_date": latest["acquisition_date"],
        "polarisation": latest["polarisation"],
        "hh_mean_db": None,       # Populated after full raster processing
        "hv_mean_db": None,       # Populated after full raster processing
        "hh_hv_ratio": None,      # Populated after full raster processing
        "granule_name": latest["granule_name"],
    }
