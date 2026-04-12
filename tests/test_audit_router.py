import asyncio
import importlib
import sys
import types
from datetime import datetime, timezone


def _install_fastapi_stub():
    fastapi_stub = types.ModuleType("fastapi")

    class _HTTPException(Exception):
        def __init__(self, status_code, detail, headers=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail
            self.headers = headers or {}

    class _APIRouter:
        def get(self, *_args, **_kwargs):
            def decorator(func):
                return func

            return decorator

        def post(self, *_args, **_kwargs):
            def decorator(func):
                return func

            return decorator

    fastapi_stub.APIRouter = _APIRouter
    fastapi_stub.Depends = lambda dependency=None: dependency
    fastapi_stub.Query = lambda default=None, **_kwargs: default
    fastapi_stub.HTTPException = _HTTPException
    fastapi_stub.status = types.SimpleNamespace(
        HTTP_200_OK=200,
        HTTP_202_ACCEPTED=202,
        HTTP_400_BAD_REQUEST=400,
        HTTP_403_FORBIDDEN=403,
        HTTP_404_NOT_FOUND=404,
        HTTP_409_CONFLICT=409,
        HTTP_500_INTERNAL_SERVER_ERROR=500,
        HTTP_503_SERVICE_UNAVAILABLE=503,
    )
    sys.modules["fastapi"] = fastapi_stub


def _install_supporting_stubs(stores, land_record, zones, tree_scans):
    starlette_stub = types.ModuleType("starlette.concurrency")

    async def _run_in_threadpool(func, *args, **kwargs):
        return func(*args, **kwargs)

    starlette_stub.run_in_threadpool = _run_in_threadpool
    sys.modules["starlette.concurrency"] = starlette_stub

    audit_models_stub = types.ModuleType("models.audit")

    class _SimpleModel:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    audit_models_stub.AuditHistoryItem = _SimpleModel
    audit_models_stub.AuditHistoryResponse = _SimpleModel
    audit_models_stub.AuditSubmitRequest = _SimpleModel
    audit_models_stub.AuditSubmitResponse = _SimpleModel
    audit_models_stub.AuditZonesResponse = _SimpleModel
    audit_models_stub.ZoneResponse = _SimpleModel
    audit_models_stub.AuditResultResponse = dict
    sys.modules["models.audit"] = audit_models_stub

    app_database_stub = types.ModuleType("app.database")

    async def fetch_land_parcel_record(_land_id):
        return dict(land_record)

    async def list_sampling_zones_for_audit(_audit_id):
        return [dict(zone) for zone in zones]

    async def list_tree_scans_for_audit(_audit_id):
        return [dict(scan) for scan in tree_scans]

    async def delete_tree_scan_records_for_audit(_audit_id):
        return 0

    async def insert_sampling_zone_records(*_args, **_kwargs):
        return True

    async def insert_tree_scan_record(*_args, **_kwargs):
        return None

    async def land_contains_point(*_args, **_kwargs):
        return True

    class _Response:
        def __init__(self, data):
            self.data = data

    class _FakeTableQuery:
        def __init__(self, table_name, rows):
            self._table_name = table_name
            self._rows = rows
            self._filters = []
            self._maybe_single = False
            self._limit = None

        def select(self, *_args, **_kwargs):
            return self

        def eq(self, field, value):
            self._filters.append((field, value))
            return self

        def order(self, *_args, **_kwargs):
            return self

        def limit(self, value):
            self._limit = value
            return self

        def maybe_single(self):
            self._maybe_single = True
            return self

        def execute(self):
            filtered_rows = [
                row.copy()
                for row in self._rows.get(self._table_name, [])
                if all(row.get(field) == value for field, value in self._filters)
            ]
            if self._limit is not None:
                filtered_rows = filtered_rows[: self._limit]
            if self._maybe_single:
                return _Response(filtered_rows[0] if filtered_rows else None)
            return _Response(filtered_rows)

    class _FakeSupabaseClient:
        def __init__(self, rows):
            self._rows = rows

        def table(self, table_name):
            return _FakeTableQuery(table_name, self._rows)

    app_database_stub.delete_tree_scan_records_for_audit = delete_tree_scan_records_for_audit
    app_database_stub.fetch_land_parcel_record = fetch_land_parcel_record
    app_database_stub.insert_sampling_zone_records = insert_sampling_zone_records
    app_database_stub.insert_tree_scan_record = insert_tree_scan_record
    app_database_stub.land_contains_point = land_contains_point
    app_database_stub.list_sampling_zones_for_audit = list_sampling_zones_for_audit
    app_database_stub.list_tree_scans_for_audit = list_tree_scans_for_audit
    app_database_stub.supabase_client = _FakeSupabaseClient(stores)
    sys.modules["app.database"] = app_database_stub

    dependencies_stub = types.ModuleType("app.dependencies")
    dependencies_stub.get_current_user = lambda: None
    sys.modules["app.dependencies"] = dependencies_stub

    rate_limit_stub = types.ModuleType("app.rate_limit")

    class _RateLimitSpec:
        def __init__(self, scope, limit, window_seconds, error_message):
            self.scope = scope
            self.limit = limit
            self.window_seconds = window_seconds
            self.error_message = error_message

    rate_limit_stub.RateLimitSpec = _RateLimitSpec
    rate_limit_stub.enforce_rate_limit = lambda *_args, **_kwargs: None
    sys.modules["app.rate_limit"] = rate_limit_stub

    zone_service_stub = types.ModuleType("services.zone_generation_service")
    zone_service_stub.generate_sampling_zones = lambda *_args, **_kwargs: []
    sys.modules["services.zone_generation_service"] = zone_service_stub

    fusion_engine_stub = types.ModuleType("services.fusion_engine")
    fusion_engine_stub.normalise_species_name = lambda value: value
    fusion_engine_stub.wood_density_for_species = lambda _value: 0.7
    sys.modules["services.fusion_engine"] = fusion_engine_stub

    ipfs_service_stub = types.ModuleType("services.ipfs_service")
    ipfs_service_stub.to_gateway_url = lambda value: value
    sys.modules["services.ipfs_service"] = ipfs_service_stub


def _load_audit_module(stores, land_record, zones, tree_scans):
    _install_fastapi_stub()
    _install_supporting_stubs(stores, land_record, zones, tree_scans)
    sys.modules.pop("routers.audit", None)
    return importlib.import_module("routers.audit")


def test_get_audit_zones_resumes_existing_processing_audit():
    audit_year = datetime.now(timezone.utc).year
    zones = [
        {
            "id": "zone-a",
            "zone_label": "A",
            "centre_gps": {"lat": 18.5204, "lng": 73.8567},
            "radius_metres": 11.0,
            "zone_type": "high_density",
            "sequence_order": 1,
            "gedi_available": True,
        },
        {
            "id": "zone-b",
            "zone_label": "B",
            "centre_gps": {"lat": 18.5209, "lng": 73.8575},
            "radius_metres": 11.0,
            "zone_type": "medium_density",
            "sequence_order": 2,
            "gedi_available": False,
        },
    ]
    audit_module = _load_audit_module(
        {
            "carbon_audits": [
                {
                    "id": "audit-1",
                    "land_id": "land-1",
                    "user_id": "user-1",
                    "audit_year": audit_year,
                    "status": "PROCESSING",
                }
            ]
        },
        {
            "id": "land-1",
            "user_id": "user-1",
            "boundary_geojson": {"type": "Polygon", "coordinates": []},
        },
        zones,
        [],
    )

    response = asyncio.run(
        audit_module.get_audit_zones(
            land_id="land-1",
            current_user={"id": "user-1"},
        )
    )

    assert response.audit_id == "audit-1"
    assert len(response.zones) == 2
    assert response.min_trees_required == 6
    assert response.walking_path_metres > 0


def test_get_audit_result_returns_awaiting_samples_phase_for_resumable_audit():
    audit_module = _load_audit_module(
        {
            "carbon_audits": [
                {
                    "id": "audit-1",
                    "land_id": "land-1",
                    "user_id": "user-1",
                    "status": "PROCESSING",
                    "trees_scanned_count": None,
                    "error": None,
                }
            ]
        },
        {
            "id": "land-1",
            "user_id": "user-1",
            "boundary_geojson": {"type": "Polygon", "coordinates": []},
        },
        [
            {
                "id": "zone-a",
                "zone_label": "A",
                "centre_gps": {"lat": 18.5204, "lng": 73.8567},
                "radius_metres": 11.0,
                "zone_type": "high_density",
                "sequence_order": 1,
                "gedi_available": True,
            },
            {
                "id": "zone-b",
                "zone_label": "B",
                "centre_gps": {"lat": 18.5209, "lng": 73.8575},
                "radius_metres": 11.0,
                "zone_type": "medium_density",
                "sequence_order": 2,
                "gedi_available": False,
            },
        ],
        [],
    )

    response = asyncio.run(
        audit_module.get_audit_result(
            audit_id="audit-1",
            current_user={"id": "user-1"},
        )
    )

    assert response["status"] == "PROCESSING"
    assert response["phase"] == "AWAITING_SAMPLES"
    assert response["trees_submitted"] == 0
    assert response["min_trees_required"] == 6
    assert response["zones_total"] == 2
    assert response["zones_completed"] == 0
    assert response["can_resume_scanning"] is True


def test_get_audit_result_returns_calculating_phase_after_submission():
    audit_module = _load_audit_module(
        {
            "carbon_audits": [
                {
                    "id": "audit-1",
                    "land_id": "land-1",
                    "user_id": "user-1",
                    "status": "CALCULATING",
                    "trees_scanned_count": 6,
                    "error": None,
                }
            ]
        },
        {
            "id": "land-1",
            "user_id": "user-1",
            "boundary_geojson": {"type": "Polygon", "coordinates": []},
        },
        [
            {
                "id": "zone-a",
                "zone_label": "A",
                "centre_gps": {"lat": 18.5204, "lng": 73.8567},
                "radius_metres": 11.0,
                "zone_type": "high_density",
                "sequence_order": 1,
                "gedi_available": True,
            },
            {
                "id": "zone-b",
                "zone_label": "B",
                "centre_gps": {"lat": 18.5209, "lng": 73.8575},
                "radius_metres": 11.0,
                "zone_type": "medium_density",
                "sequence_order": 2,
                "gedi_available": False,
            },
        ],
        [
            {"id": "scan-1", "zone_id": "zone-a"},
            {"id": "scan-2", "zone_id": "zone-a"},
            {"id": "scan-3", "zone_id": "zone-a"},
            {"id": "scan-4", "zone_id": "zone-b"},
            {"id": "scan-5", "zone_id": "zone-b"},
            {"id": "scan-6", "zone_id": "zone-b"},
        ],
    )

    response = asyncio.run(
        audit_module.get_audit_result(
            audit_id="audit-1",
            current_user={"id": "user-1"},
        )
    )

    assert response["status"] == "CALCULATING"
    assert response["phase"] == "CALCULATING"
    assert response["trees_submitted"] == 6
    assert response["zones_total"] == 2
    assert response["zones_completed"] == 2
    assert response["can_resume_scanning"] is False