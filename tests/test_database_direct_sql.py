import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import app.database as database


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
