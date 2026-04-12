import importlib
import sys
import types


def _install_fastapi_stub():
    fastapi_stub = types.ModuleType("fastapi")

    class _APIRouter:
        def get(self, *_args, **_kwargs):
            def decorator(func):
                return func

            return decorator

        def post(self, *_args, **_kwargs):
            def decorator(func):
                return func

            return decorator

        def patch(self, *_args, **_kwargs):
            def decorator(func):
                return func

            return decorator

    class _HTTPException(Exception):
        def __init__(self, status_code, detail, headers=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail
            self.headers = headers or {}

    fastapi_stub.APIRouter = _APIRouter
    fastapi_stub.Depends = lambda dependency=None: dependency
    fastapi_stub.File = lambda default=None, **_kwargs: default
    fastapi_stub.Form = lambda default=None, **_kwargs: default
    fastapi_stub.Query = lambda default=None, **_kwargs: default
    fastapi_stub.UploadFile = object
    fastapi_stub.HTTPException = _HTTPException
    fastapi_stub.status = types.SimpleNamespace(
        HTTP_400_BAD_REQUEST=400,
        HTTP_409_CONFLICT=409,
        HTTP_415_UNSUPPORTED_MEDIA_TYPE=415,
        HTTP_413_REQUEST_ENTITY_TOO_LARGE=413,
        HTTP_422_UNPROCESSABLE_ENTITY=422,
        HTTP_500_INTERNAL_SERVER_ERROR=500,
        HTTP_201_CREATED=201,
        HTTP_200_OK=200,
    )
    sys.modules["fastapi"] = fastapi_stub


def _install_land_supporting_stubs():
    starlette_stub = types.ModuleType("starlette.concurrency")
    starlette_stub.run_in_threadpool = lambda func, *args, **kwargs: func(*args, **kwargs)
    sys.modules["starlette.concurrency"] = starlette_stub

    redis_stub = types.ModuleType("redis")

    class _RedisStub:
        @classmethod
        def from_url(cls, *_args, **_kwargs):
            raise RuntimeError("redis unavailable")

    redis_stub.Redis = _RedisStub
    sys.modules["redis"] = redis_stub

    config_stub = types.ModuleType("app.config")
    config_stub.settings = types.SimpleNamespace(
        REDIS_URL="redis://localhost:6379/0",
        SUPABASE_URL="https://example.supabase.co",
    )
    sys.modules["app.config"] = config_stub

    database_stub = types.ModuleType("app.database")
    database_stub.analyse_boundary_geojson = None
    database_stub.insert_land_parcel_record = None
    database_stub.list_land_parcels_for_user = None
    database_stub.supabase_client = None
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

    land_models_stub = types.ModuleType("models.land")

    class _SimpleModel:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    for name in (
        "BoundaryFetchResponse",
        "DocumentUploadResponse",
        "LandListItem",
        "LandListResponse",
        "LandRegisterRequest",
        "LandRegisterResponse",
        "LandUpdateRequest",
        "LandUpdateResponse",
    ):
        setattr(land_models_stub, name, _SimpleModel)
    sys.modules["models.land"] = land_models_stub

    services_stub = types.ModuleType("services")
    land_boundary_stub = types.ModuleType("services.land_boundary_service")
    ocr_stub = types.ModuleType("services.ocr_service")
    satellite_stub = types.ModuleType("services.satellite_service")
    services_stub.land_boundary_service = land_boundary_stub
    services_stub.ocr_service = ocr_stub
    services_stub.satellite_service = satellite_stub
    sys.modules["services"] = services_stub
    sys.modules["services.land_boundary_service"] = land_boundary_stub
    sys.modules["services.ocr_service"] = ocr_stub
    sys.modules["services.satellite_service"] = satellite_stub


def _load_land_module():
    _install_fastapi_stub()
    _install_land_supporting_stubs()
    sys.modules.pop("routers.land", None)
    return importlib.import_module("routers.land")


def test_processing_audit_without_samples_is_reported_as_awaiting_samples():
    land = _load_land_module()

    summary = land._summarise_current_audit(
        {
            "id": "audit-1",
            "status": "PROCESSING",
            "trees_scanned_count": None,
            "error": None,
        }
    )

    assert summary["status"] == "AWAITING_SAMPLES"
    assert summary["backend_status"] == "PROCESSING"
    assert summary["phase"] == "AWAITING_SAMPLES"
    assert summary["can_resume_scanning"] is True


def test_processing_audit_with_error_is_reported_as_retry_submission():
    land = _load_land_module()

    summary = land._summarise_current_audit(
        {
            "id": "audit-1",
            "status": "PROCESSING",
            "trees_scanned_count": 0,
            "error": "Fusion task queue is temporarily unavailable.",
        }
    )

    assert summary["status"] == "RETRY_SUBMISSION"
    assert summary["backend_status"] == "PROCESSING"
    assert summary["phase"] == "RETRY_SUBMISSION"
    assert summary["can_resume_scanning"] is True