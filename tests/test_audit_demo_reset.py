import importlib
import sys
import types


def _load_audit_module_for_demo_helper():
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

    database_stub = types.ModuleType("app.database")
    database_stub.delete_tree_scan_records_for_audit = lambda *_args, **_kwargs: None
    database_stub.fetch_land_parcel_record = lambda *_args, **_kwargs: None
    database_stub.insert_sampling_zone_records = lambda *_args, **_kwargs: None
    database_stub.insert_tree_scan_record = lambda *_args, **_kwargs: None
    database_stub.land_contains_point = lambda *_args, **_kwargs: True
    database_stub.list_sampling_zones_for_audit = lambda *_args, **_kwargs: []
    database_stub.list_tree_scans_for_audit = lambda *_args, **_kwargs: []
    database_stub.supabase_client = types.SimpleNamespace(table=lambda *_args, **_kwargs: None)
    sys.modules["app.database"] = database_stub

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

    models_audit_stub = types.ModuleType("models.audit")

    class _SimpleModel:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    models_audit_stub.AuditHistoryItem = _SimpleModel
    models_audit_stub.AuditHistoryResponse = _SimpleModel
    models_audit_stub.AuditResultResponse = dict
    models_audit_stub.AuditSubmitRequest = _SimpleModel
    models_audit_stub.AuditSubmitResponse = _SimpleModel
    models_audit_stub.AuditZonesResponse = _SimpleModel
    models_audit_stub.ZoneResponse = _SimpleModel
    sys.modules["models.audit"] = models_audit_stub

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

    demo_config_stub = types.ModuleType("demo.config")
    demo_config_stub.is_demo_uid = lambda firebase_uid: firebase_uid == "demo-uid"
    sys.modules["demo.config"] = demo_config_stub

    calls = []
    demo_middleware_stub = types.ModuleType("demo.middleware")
    demo_middleware_stub.invalidate_demo_session = lambda firebase_uid: calls.append(firebase_uid)
    sys.modules["demo.middleware"] = demo_middleware_stub

    sys.modules.pop("routers.audit", None)
    module = importlib.import_module("routers.audit")
    return module, calls


def test_demo_accounts_are_invalidated_after_audit_start():
    audit_module, calls = _load_audit_module_for_demo_helper()

    audit_module._maybe_invalidate_demo_checkpoint({"firebase_uid": "demo-uid"})
    audit_module._maybe_invalidate_demo_checkpoint({"firebase_uid": "regular-user"})

    assert calls == ["demo-uid"]