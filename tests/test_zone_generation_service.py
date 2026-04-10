import os
import sys
import types


def _point_on_segment(point_x, point_y, start, end, epsilon=1e-9):
    start_x, start_y = start
    end_x, end_y = end
    cross = (point_y - start_y) * (end_x - start_x) - (point_x - start_x) * (end_y - start_y)
    if abs(cross) > epsilon:
        return False

    dot = (point_x - start_x) * (end_x - start_x) + (point_y - start_y) * (end_y - start_y)
    if dot < -epsilon:
        return False

    squared_length = (end_x - start_x) ** 2 + (end_y - start_y) ** 2
    return dot <= squared_length + epsilon


class _Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y


class _Polygon:
    def __init__(self, coordinates):
        self._ring = coordinates[0]
        x_values = [point[0] for point in self._ring]
        y_values = [point[1] for point in self._ring]
        self.bounds = (min(x_values), min(y_values), max(x_values), max(y_values))

    def covers(self, point):
        for start, end in zip(self._ring, self._ring[1:]):
            if _point_on_segment(point.x, point.y, start, end):
                return True

        inside = False
        point_count = len(self._ring)
        for index in range(point_count - 1):
            start_x, start_y = self._ring[index]
            end_x, end_y = self._ring[index + 1]
            intersects = ((start_y > point.y) != (end_y > point.y)) and (
                point.x < (end_x - start_x) * (point.y - start_y) / (end_y - start_y + 1e-12) + start_x
            )
            if intersects:
                inside = not inside

        return inside

    def representative_point(self):
        min_x, min_y, max_x, max_y = self.bounds
        candidate_points = [
            _Point((min_x + max_x) / 2, (min_y + max_y) / 2),
        ]

        for x_step in range(1, 11):
            for y_step in range(1, 11):
                candidate_points.append(
                    _Point(
                        min_x + ((max_x - min_x) * x_step / 11),
                        min_y + ((max_y - min_y) * y_step / 11),
                    )
                )

        for candidate in candidate_points:
            if self.covers(candidate):
                return candidate

        return _Point(self._ring[0][0], self._ring[0][1])


def _shape(geojson):
    if geojson.get("type") == "Polygon":
        return _Polygon(geojson["coordinates"])
    raise ValueError("Only Polygon geometry is supported by the test stub.")


shapely_stub = types.ModuleType("shapely")
shapely_geometry_stub = types.ModuleType("shapely.geometry")
shapely_geometry_stub.Point = _Point
shapely_geometry_stub.shape = _shape
shapely_stub.geometry = shapely_geometry_stub
sys.modules.setdefault("shapely", shapely_stub)
sys.modules.setdefault("shapely.geometry", shapely_geometry_stub)

Point = _Point
shape = _shape

os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")

ee_stub = types.ModuleType("ee")
ee_stub.Image = object
ee_stub.Geometry = types.SimpleNamespace(Point=lambda *_args, **_kwargs: None)
ee_stub.Number = lambda *_args, **_kwargs: types.SimpleNamespace(getInfo=lambda: 1)
ee_stub.ServiceAccountCredentials = lambda *_args, **_kwargs: None
ee_stub.Initialize = lambda *_args, **_kwargs: None
ee_stub.Reducer = types.SimpleNamespace(mean=lambda: None, count=lambda: None)
sys.modules.setdefault("ee", ee_stub)

config_stub = types.ModuleType("app.config")
config_stub.settings = types.SimpleNamespace(
    GOOGLE_APPLICATION_CREDENTIALS="",
    GOOGLE_CLOUD_PROJECT="test-project",
)
sys.modules.setdefault("app.config", config_stub)

from services import zone_generation_service


def test_determine_zone_plan_scales_with_farm_size():
    assert zone_generation_service._determine_zone_plan(0.2) == (2, 7.0)
    assert zone_generation_service._determine_zone_plan(0.35) == (3, 7.0)
    assert zone_generation_service._determine_zone_plan(1.0) == (4, 9.0)
    assert zone_generation_service._determine_zone_plan(2.5) == (5, 11.0)
    assert zone_generation_service._determine_zone_plan(5.0) == (6, 11.0)
    assert zone_generation_service._determine_zone_plan(8.5) == (7, 11.0)
    assert zone_generation_service._determine_zone_plan(12.0) == (8, 11.0)


def test_distribute_zone_counts_spreads_remainder_across_bands():
    assert zone_generation_service._distribute_zone_counts(4) == {
        "low_density": 1,
        "medium_density": 2,
        "high_density": 1,
    }
    assert zone_generation_service._distribute_zone_counts(5) == {
        "low_density": 1,
        "medium_density": 2,
        "high_density": 2,
    }
    assert zone_generation_service._distribute_zone_counts(6) == {
        "low_density": 2,
        "medium_density": 2,
        "high_density": 2,
    }


def test_order_zone_points_uses_nearest_neighbour_from_start():
    zone_points = [
        {"lat": 18.5035, "lng": 73.8685, "zone_type": "high_density"},
        {"lat": 18.5002, "lng": 73.8652, "zone_type": "medium_density"},
        {"lat": 18.5011, "lng": 73.8660, "zone_type": "low_density"},
    ]

    ordered = zone_generation_service._order_zone_points(
        zone_points,
        {"lat": 18.5000, "lng": 73.8650},
    )

    assert ordered[0]["lat"] == 18.5002
    assert ordered[0]["lng"] == 73.8652
    assert ordered[1]["lat"] == 18.5011
    assert ordered[1]["lng"] == 73.8660
    assert ordered[2]["lat"] == 18.5035
    assert ordered[2]["lng"] == 73.8685


def test_interior_fallback_points_remain_inside_concave_parcel():
    boundary_geojson = {
        "type": "Polygon",
        "coordinates": [[
            [73.8650, 18.5000],
            [73.8690, 18.5000],
            [73.8690, 18.5010],
            [73.8670, 18.5010],
            [73.8670, 18.5030],
            [73.8650, 18.5030],
            [73.8650, 18.5000],
        ]],
    }

    parcel = shape(boundary_geojson)
    points = zone_generation_service._interior_fallback_points(boundary_geojson, 4, 0.52)

    assert len(points) == 4
    for point in points:
        assert parcel.covers(Point(point["lng"], point["lat"]))