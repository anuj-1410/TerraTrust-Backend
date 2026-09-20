import asyncio
import importlib
import os
import sys
from datetime import datetime, timezone
from uuid import uuid4

os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")

for module_name in ("app.database", "app.config"):
    sys.modules.pop(module_name, None)

database = importlib.import_module("app.database")


class _FakeResult:
    def __init__(self, *, row=None, rows=None):
        self._row = row
        self._rows = rows or []

    def mappings(self):
        return self

    def first(self):
        return self._row

    def all(self):
        return self._rows


class _FakeConnection:
    def __init__(self, result):
        self._result = result

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _query, _params):
        return self._result


class _FakeEngine:
    def __init__(self, result):
        self._result = result

    def connect(self):
        return _FakeConnection(self._result)


class _SequencedConnection:
    def __init__(self, results):
        self._results = list(results)
        self.executed = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, query, params):
        self.executed.append((str(query), params))
        if self._results:
            return self._results.pop(0)
        return _FakeResult()


class _SequencedEngine:
    def __init__(self, results):
        self.connection = _SequencedConnection(results)

    def begin(self):
        return self.connection


def test_require_async_engine_raises_clear_setup_error(monkeypatch):
    monkeypatch.setattr(database, "async_engine", None)

    try:
        database._require_async_engine()
    except RuntimeError as exc:
        assert "DATABASE_URL must be configured" in str(exc)
    else:
        raise AssertionError("Expected missing async engine to raise RuntimeError.")


def test_fetch_land_parcel_record_normalises_uuid_owner_and_timestamp(monkeypatch):
    land_id = uuid4()
    user_id = uuid4()
    created_at = datetime(2026, 4, 12, 17, 59, 56, tzinfo=timezone.utc)

    fake_row = {
        "id": land_id,
        "user_id": user_id,
        "farm_name": "Main Demo Farm",
        "survey_number": "DEMO-004-47",
        "district": "Pune",
        "taluka": "Baramati",
        "village": "Morgaon",
        "state": "Maharashtra",
        "boundary_source": "MANUAL",
        "ocr_owner_name": "Demo Farmer",
        "doc_image_url": None,
        "lgd_district_code": None,
        "lgd_taluka_code": None,
        "lgd_village_code": None,
        "gis_code": None,
        "area_hectares": 5.25,
        "is_verified": True,
        "created_at": created_at,
        "boundary_geojson": '{"type":"Point","coordinates":[73.5,18.5]}',
    }

    monkeypatch.setattr(
        database,
        "_require_async_engine",
        lambda: _FakeEngine(_FakeResult(row=fake_row)),
    )

    record = asyncio.run(database.fetch_land_parcel_record(str(land_id)))

    assert record["id"] == str(land_id)
    assert record["user_id"] == str(user_id)
    assert record["created_at"] == created_at.isoformat()
    assert record["boundary_geojson"] == {"type": "Point", "coordinates": [73.5, 18.5]}


def test_list_sampling_zones_for_audit_normalises_zone_ids(monkeypatch):
    zone_id = uuid4()

    fake_rows = [
        {
            "id": zone_id,
            "zone_label": "A",
            "radius_metres": 12.5,
            "zone_type": "high_density",
            "ndvi_mean": 0.72,
            "gedi_available": True,
            "sequence_order": 1,
            "lat": 18.5123,
            "lng": 73.8123,
        }
    ]

    monkeypatch.setattr(
        database,
        "_require_async_engine",
        lambda: _FakeEngine(_FakeResult(rows=fake_rows)),
    )

    zones = asyncio.run(database.list_sampling_zones_for_audit("audit-1"))

    assert zones[0]["id"] == str(zone_id)
    assert zones[0]["centre_gps"] == {"lat": 18.5123, "lng": 73.8123}


def test_replace_tree_scan_records_returns_old_evidence_paths(monkeypatch):
    fake_engine = _SequencedEngine(
        [
            _FakeResult(row={"id": "audit-1"}),
            _FakeResult(
                rows=[
                    {"evidence_photo_path": "audit-1/old-a.jpg"},
                    {"evidence_photo_path": None},
                    {"evidence_photo_path": "audit-1/old-b.jpg"},
                ]
            ),
        ]
    )
    monkeypatch.setattr(database, "_require_async_engine", lambda: fake_engine)

    claimed, old_paths = asyncio.run(
        database.replace_tree_scan_records_for_audit(
            "audit-1",
            [
                {
                    "id": "scan-1",
                    "audit_id": "audit-1",
                    "land_id": "land-1",
                    "zone_id": "zone-1",
                    "species": "Neem",
                    "dbh_cm": 22.0,
                    "height_m": 8.0,
                    "gps": {"lat": 18.5, "lng": 73.8},
                    "gps_accuracy_m": 4.0,
                    "wood_density": 0.56,
                }
            ],
        )
    )

    assert claimed is True
    assert old_paths == ["audit-1/old-a.jpg", "audit-1/old-b.jpg"]
    assert any("DELETE FROM ar_tree_scans" in query for query, _params in fake_engine.connection.executed)
