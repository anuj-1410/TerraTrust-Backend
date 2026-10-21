"""PostGIS geometry validation and actual geodesic area; no farmer defaults."""
import json
import math

from shapely.geometry import shape, mapping, MultiPolygon

from app.institution_errors import fail
from models.institution.common import digest, normalize


def polygon(geometry):
    if not isinstance(geometry, dict) or set(geometry) != {"type", "coordinates"} or geometry["type"] not in {"Polygon", "MultiPolygon"}:
        fail(422, "INVALID_GEOMETRY", "Supply an EPSG:4326 Polygon or MultiPolygon.")
    def coordinates(node):
        if isinstance(node, (list, tuple)) and len(node) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in node):
            lon, lat = node
            if not math.isfinite(lon) or not math.isfinite(lat) or not -180 <= lon <= 180 or not -90 <= lat <= 90:
                fail(422, "INVALID_GEOMETRY", "Coordinates must be finite longitude,latitude.")
            return
        if not isinstance(node, (list, tuple)) or not node:
            fail(422, "INVALID_GEOMETRY", "Coordinate arrays cannot be empty.")
        for item in node:
            coordinates(item)
    coordinates(geometry["coordinates"])
    try:
        value = shape(geometry)
    except Exception:
        fail(422, "INVALID_GEOMETRY", "Malformed polygon coordinates.")
    if value.is_empty or not value.is_valid:
        fail(422, "INVALID_GEOMETRY", "Polygon is empty or self-intersecting; correct it before saving.")
    if value.bounds[2] - value.bounds[0] >= 180:
        fail(422, "INVALID_GEOMETRY", "Antimeridian boundaries require separately reviewed handling.")
    if value.geom_type == "Polygon":
        value = MultiPolygon([value])
    return json.loads(json.dumps(mapping(value)))


async def measure(repo, geometry):
    normalized = polygon(geometry)
    row = await repo.one("SELECT ST_IsValid(g) AS valid, ST_Area(g::geography)/10000.0 AS area FROM (SELECT ST_SetSRID(ST_GeomFromGeoJSON(:geo),4326) AS g) s", {"geo": json.dumps(normalized)})
    if not row["valid"] or row["area"] <= 0:
        fail(422, "INVALID_GEOMETRY", "Boundary has no valid measured area.")
    return normalized, row["area"]


def point(geometry):
    if not isinstance(geometry, dict) or set(geometry) != {"type", "coordinates"} or geometry["type"] != "Point":
        fail(422, "INVALID_GEOMETRY", "Expected a GeoJSON Point.")
    coords = geometry["coordinates"]
    if not isinstance(coords, list) or len(coords) != 2 or any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in coords) or not -180 <= coords[0] <= 180 or not -90 <= coords[1] <= 90:
        fail(422, "INVALID_GEOMETRY", "Expected finite longitude,latitude.")
    return geometry
