"""
Zone generation service — NDVI-based sampling zone placement using GEE.

Generates 2-8 representative sampling zones (high / medium / low NDVI)
within a land parcel so the farmer walks a stratified path.
"""

import logging
import math
import uuid
from typing import Any, Dict, List

import ee
from shapely.geometry import Point, shape

from app.gee import ensure_gee_initialized

logger = logging.getLogger("terratrust.zones")

ACRE_TO_HECTARES = 0.40468564224


def _ensure_gee_initialised() -> None:
    """Initialise GEE if not already done (idempotent)."""
    ensure_gee_initialized()


def _distance_metres(first: Dict[str, float], second: Dict[str, float]) -> float:
    """Approximate straight-line distance between two lat/lng points."""
    dlat = (second["lat"] - first["lat"]) * 111_320
    dlng = (second["lng"] - first["lng"]) * 111_320 * math.cos(
        math.radians((first["lat"] + second["lat"]) / 2)
    )
    return math.sqrt(dlat**2 + dlng**2)


def _label_for_index(index: int) -> str:
    """Convert a zero-based index into spreadsheet-style labels."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    label = ""
    current = index

    while True:
        label = alphabet[current % 26] + label
        current = current // 26 - 1
        if current < 0:
            return label


def _determine_zone_plan(area_hectares: float) -> tuple[int, float]:
    """Choose zone count and radius from the documented farm-size bands.

    TerraTrust v3.1 specifies zone-count ranges by acreage, so this helper
    scales deterministically within each range instead of returning the same
    fixed count for every parcel size in a band.
    """
    area_acres = area_hectares / ACRE_TO_HECTARES

    if area_acres <= 0.5:
        return 2, 7.0
    if area_acres <= 1.0:
        return 3, 7.0
    if area_acres <= 2.0:
        return 3, 9.0
    if area_acres <= 3.0:
        return 4, 9.0
    if area_acres <= 5.0:
        return 4, 11.0
    if area_acres <= 7.5:
        return 5, 11.0
    if area_acres <= 10.0:
        return 6, 11.0
    if area_acres <= 15.0:
        return 6, 11.0
    if area_acres <= 25.0:
        return 7, 11.0
    return 8, 11.0


def _distribute_zone_counts(zone_count: int) -> Dict[str, int]:
    """Spread the requested zones across low, medium, and high NDVI bands."""
    counts = {
        "low_density": zone_count // 3,
        "medium_density": zone_count // 3,
        "high_density": zone_count // 3,
    }
    remainder = zone_count % 3
    for zone_type in ("medium_density", "high_density", "low_density")[:remainder]:
        counts[zone_type] += 1
    return counts


def _classify_zone_type(ndvi_mean: float | None, p25: float, p75: float) -> str:
    """Map an NDVI mean into the documented density labels."""
    if ndvi_mean is None:
        return "medium_density"
    if ndvi_mean >= p75:
        return "high_density"
    if ndvi_mean < p25:
        return "low_density"
    return "medium_density"


def _sample_zone_points(
    ndvi_img: ee.Image,
    region: ee.Geometry,
    count: int,
    *,
    seed: int,
    mask: ee.Image | None = None,
) -> List[Dict[str, float | None]]:
    """Sample up to ``count`` point geometries from an NDVI image."""
    if count <= 0:
        return []

    sample_image = ndvi_img.rename("NDVI")
    if mask is not None:
        sample_image = sample_image.updateMask(mask)

    sample_collection = sample_image.sample(
        region=region,
        scale=10,
        numPixels=max(count * 3, count),
        seed=seed,
        geometries=True,
    )

    points: List[Dict[str, float | None]] = []
    seen: set[tuple[float, float]] = set()
    info = sample_collection.getInfo() or {}

    for feature in info.get("features", []):
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates") or []
        if len(coordinates) < 2:
            continue

        lng = float(coordinates[0])
        lat = float(coordinates[1])
        key = (round(lat, 6), round(lng, 6))
        if key in seen:
            continue

        seen.add(key)
        ndvi_mean = (feature.get("properties") or {}).get("NDVI")
        points.append(
            {
                "lat": lat,
                "lng": lng,
                "ndvi_mean": float(ndvi_mean) if ndvi_mean is not None else None,
            }
        )
        if len(points) >= count:
            break

    return points


def _region_centroid_point(ndvi_img: ee.Image, region: ee.Geometry) -> Dict[str, float | None]:
    """Return the parcel centroid and parcel-wide mean NDVI."""
    centroid = region.centroid(1).coordinates().getInfo()
    stats = ndvi_img.reduceRegion(
        reducer=ee.Reducer.mean(),
        geometry=region,
        scale=10,
        maxPixels=1e8,
    ).getInfo()
    ndvi_mean = (stats or {}).get("NDVI")
    return {
        "lat": float(centroid[1]),
        "lng": float(centroid[0]),
        "ndvi_mean": float(ndvi_mean) if ndvi_mean is not None else None,
    }


def _representative_boundary_point(
    boundary_geojson: Dict[str, Any],
    ndvi_mean: float | None,
) -> Dict[str, float | None]:
    """Return a guaranteed interior point for the parcel boundary."""
    parcel = shape(boundary_geojson)
    representative_point = parcel.representative_point()
    return {
        "lat": float(representative_point.y),
        "lng": float(representative_point.x),
        "ndvi_mean": ndvi_mean,
    }


def _interior_fallback_points(
    boundary_geojson: Dict[str, Any],
    count: int,
    ndvi_mean: float | None,
) -> List[Dict[str, float | None]]:
    """Generate last-resort fallback points that still remain inside the parcel."""
    if count <= 0:
        return []

    parcel = shape(boundary_geojson)
    anchor = parcel.representative_point()
    min_x, min_y, max_x, max_y = parcel.bounds
    span = max(max_x - min_x, max_y - min_y, 1e-5)
    step = min(max(span * 0.12, 1e-5), 2e-4)
    offsets = [
        (0, 0),
        (1, 0),
        (-1, 0),
        (0, 1),
        (0, -1),
        (1, 1),
        (1, -1),
        (-1, 1),
        (-1, -1),
        (2, 0),
        (-2, 0),
        (0, 2),
        (0, -2),
    ]

    fallback_points: List[Dict[str, float | None]] = []
    seen: set[tuple[float, float]] = set()

    for scale in range(1, 6):
        for dx, dy in offsets:
            candidate = Point(anchor.x + (dx * step * scale), anchor.y + (dy * step * scale))
            if not parcel.covers(candidate):
                continue

            key = (round(candidate.y, 6), round(candidate.x, 6))
            if key in seen:
                continue

            seen.add(key)
            fallback_points.append(
                {
                    "lat": float(candidate.y),
                    "lng": float(candidate.x),
                    "ndvi_mean": ndvi_mean,
                }
            )
            if len(fallback_points) >= count:
                return fallback_points

    if not fallback_points:
        fallback_points.append(
            {
                "lat": float(anchor.y),
                "lng": float(anchor.x),
                "ndvi_mean": ndvi_mean,
            }
        )

    while len(fallback_points) < count:
        fallback_points.append(dict(fallback_points[len(fallback_points) % len(fallback_points)]))

    return fallback_points[:count]


def _order_zone_points(
    zone_points: List[Dict[str, Any]],
    start_point: Dict[str, float],
) -> List[Dict[str, Any]]:
    """Build a deterministic nearest-neighbour walking order."""
    if not zone_points:
        return []

    remaining = [dict(point) for point in zone_points]
    ordered: List[Dict[str, Any]] = []

    first_point = min(remaining, key=lambda point: _distance_metres(point, start_point))
    remaining.remove(first_point)
    ordered.append(first_point)

    while remaining:
        next_point = min(
            remaining,
            key=lambda point: _distance_metres(point, ordered[-1]),
        )
        remaining.remove(next_point)
        ordered.append(next_point)

    return ordered


def _path_length_metres(zone_points: List[Dict[str, Any]]) -> float:
    """Return total nearest-neighbour walking distance across zone centres."""
    total_walk = 0.0
    for index in range(len(zone_points) - 1):
        total_walk += _distance_metres(zone_points[index], zone_points[index + 1])
    return round(total_walk, 1)


def _zone_gedi_available(
    gedi_image: ee.Image,
    lat: float,
    lng: float,
    radius_metres: float,
) -> bool:
    """Check GEDI footprint availability for a specific zone footprint."""
    zone_region = ee.Geometry.Point([lng, lat]).buffer(radius_metres)
    zone_stats = gedi_image.reduceRegion(
        reducer=ee.Reducer.count(),
        geometry=zone_region,
        scale=25,
        maxPixels=1e8,
    ).getInfo()
    return ((zone_stats or {}).get("rh98", 0) or 0) > 0


def generate_sampling_zones(
    land_id: str,
    boundary_geojson: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Generate NDVI-stratified sampling zones inside the land boundary.

    Parameters
    ----------
    land_id : str
        UUID of the registered land parcel.
    boundary_geojson : dict
        GeoJSON geometry (``Polygon`` or ``MultiPolygon``).

    Returns
    -------
    list[dict]
        Each dict contains: zone_id, label, centre_gps,
        radius_metres, zone_type, sequence_order, gedi_available.
    """
    _ensure_gee_initialised()

    coords = boundary_geojson.get("coordinates", [])
    geom_type = boundary_geojson.get("type", "Polygon")
    if geom_type == "Polygon":
        region = ee.Geometry.Polygon(coords)
    elif geom_type == "MultiPolygon":
        region = ee.Geometry.MultiPolygon(coords)
    else:
        raise ValueError(f"Unsupported geometry type: {geom_type}")

    from datetime import datetime, timedelta

    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=180)

    s2 = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(region)
        .filterDate(start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 20))
        .median()
    )

    ndvi = s2.normalizedDifference(["B8", "B4"]).rename("NDVI")

    percentiles = ndvi.reduceRegion(
        reducer=ee.Reducer.percentile([25, 50, 75]),
        geometry=region,
        scale=10,
        maxPixels=1e8,
    ).getInfo()
    percentiles = percentiles or {}

    p25_value = percentiles.get("NDVI_p25")
    p75_value = percentiles.get("NDVI_p75")
    p25 = float(p25_value) if p25_value is not None else 0.2
    p75 = float(p75_value) if p75_value is not None else 0.6

    if p75 <= p25:
        p25 = max(-1.0, p25 - 0.05)
        p75 = min(1.0, p75 + 0.05)

    area_ha = float(region.area(1).getInfo()) / 10_000
    target_zone_count, radius_m = _determine_zone_plan(area_ha)
    zone_counts = _distribute_zone_counts(target_zone_count)
    zone_masks = {
        "high_density": ndvi.gte(p75),
        "medium_density": ndvi.gte(p25).And(ndvi.lt(p75)),
        "low_density": ndvi.lt(p25),
    }
    required_zone_types = ("high_density", "medium_density", "low_density")

    sampled_zone_points: List[Dict[str, Any]] = []
    zone_type_counts = {zone_type: 0 for zone_type in required_zone_types}
    seen_points: set[tuple[float, float]] = set()

    def append_zone_point(point: Dict[str, Any], zone_type: str) -> bool:
        key = (round(point["lat"], 6), round(point["lng"], 6))
        if key in seen_points:
            return False

        seen_points.add(key)
        sampled_zone_points.append({**point, "zone_type": zone_type})
        zone_type_counts[zone_type] += 1
        return True

    for offset, zone_type in enumerate(required_zone_types):
        class_points = _sample_zone_points(
            ndvi,
            region,
            zone_counts[zone_type],
            seed=17 + offset,
            mask=zone_masks[zone_type],
        )
        for point in class_points:
            append_zone_point(point, zone_type)

    if len(sampled_zone_points) < target_zone_count:
        supplemental_points = _sample_zone_points(
            ndvi,
            region,
            target_zone_count * 4,
            seed=97,
        )
        for point in supplemental_points:
            zone_type = _classify_zone_type(point.get("ndvi_mean"), p25, p75)
            if zone_type_counts[zone_type] >= zone_counts[zone_type]:
                continue
            if not append_zone_point(point, zone_type):
                continue
            if len(sampled_zone_points) >= target_zone_count:
                break

    centroid_point = _region_centroid_point(ndvi, region)
    fallback_points = [
        centroid_point,
        *_interior_fallback_points(
            boundary_geojson,
            max(target_zone_count * 2, target_zone_count - len(sampled_zone_points)),
            centroid_point.get("ndvi_mean"),
        ),
    ]
    for zone_type in required_zone_types:
        while zone_type_counts[zone_type] < zone_counts[zone_type]:
            added = False
            for point in fallback_points:
                if append_zone_point(point, zone_type):
                    added = True
                    break

            if added:
                continue

            extra_points = _interior_fallback_points(
                boundary_geojson,
                target_zone_count * 2,
                centroid_point.get("ndvi_mean"),
            )
            fallback_points.extend(extra_points)
            for point in extra_points:
                if append_zone_point(point, zone_type):
                    added = True
                    break

            if not added:
                raise ValueError("Failed to generate sufficient sampling zone points inside the parcel.")

    ordered_seed_points = sampled_zone_points[:target_zone_count]
    if len(ordered_seed_points) != target_zone_count:
        raise ValueError("Failed to generate the documented number of sampling zones.")

    gedi_image = None
    try:
        gedi_image = (
            ee.ImageCollection("LARSE/GEDI/GEDI02_A_002_MONTHLY")
            .filterBounds(region)
            .select(["rh98"])
            .mean()
        )
    except Exception:
        logger.warning("GEDI availability check failed — assuming unavailable.")

    route_start_point = _representative_boundary_point(
        boundary_geojson,
        centroid_point.get("ndvi_mean"),
    )
    route_start = {
        "lat": float(route_start_point["lat"] or 0.0),
        "lng": float(route_start_point["lng"] or 0.0),
    }
    ordered_zone_points = _order_zone_points(ordered_seed_points, route_start)
    total_walk = _path_length_metres(ordered_zone_points)

    zones: List[Dict[str, Any]] = []
    for index, point in enumerate(ordered_zone_points):
        gedi_available = False
        if gedi_image is not None:
            try:
                gedi_available = _zone_gedi_available(
                    gedi_image,
                    point["lat"],
                    point["lng"],
                    radius_m,
                )
            except Exception as exc:
                logger.debug("GEDI zone availability check failed: %s", exc)

        zones.append(
            {
                "zone_id": str(uuid.uuid4()),
                "label": _label_for_index(index),
                "centre_gps": {"lat": point["lat"], "lng": point["lng"]},
                "radius_metres": radius_m,
                "zone_type": point["zone_type"],
                "ndvi_mean": point.get("ndvi_mean"),
                "sequence_order": index + 1,
                "gedi_available": gedi_available,
                "walking_path_metres": total_walk,
            }
        )

    logger.info(
        "Generated %d zones for land %s (area=%.2f ha, radius=%.0f m)",
        len(zones),
        land_id,
        area_ha,
        radius_m,
    )
    return zones
