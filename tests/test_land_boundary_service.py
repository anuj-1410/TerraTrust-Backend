import importlib
import os
import re
import sys
import types


os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")


def _load_land_boundary_service_module():
    original_modules = {
        name: sys.modules.get(name)
        for name in (
            "httpx",
            "cv2",
            "numpy",
            "redis",
            "playwright",
            "playwright.async_api",
            "app.config",
            "app.database",
            "services.ocr_service",
            "services.satellite_service",
        )
    }

    httpx_stub = types.ModuleType("httpx")

    class _HTTPError(Exception):
        pass

    class _HTTPStatusError(_HTTPError):
        def __init__(self, message="", request=None, response=None):
            super().__init__(message)
            self.request = request
            self.response = response

    class _AsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *_args, **_kwargs):
            raise RuntimeError("httpx AsyncClient is not exercised in this unit test")

    httpx_stub.AsyncClient = _AsyncClient
    httpx_stub.HTTPError = _HTTPError
    httpx_stub.HTTPStatusError = _HTTPStatusError
    sys.modules["httpx"] = httpx_stub

    cv2_stub = types.ModuleType("cv2")
    sys.modules.setdefault("cv2", cv2_stub)

    numpy_stub = types.ModuleType("numpy")
    numpy_stub.ndarray = object
    sys.modules.setdefault("numpy", numpy_stub)

    redis_stub = types.ModuleType("redis")

    class _RedisStub:
        @classmethod
        def from_url(cls, *_args, **_kwargs):
            raise RuntimeError("redis unavailable in unit test")

    redis_stub.Redis = _RedisStub
    sys.modules.setdefault("redis", redis_stub)

    playwright_async_api_stub = types.ModuleType("playwright.async_api")
    playwright_async_api_stub.Page = object
    playwright_async_api_stub.TimeoutError = TimeoutError
    playwright_async_api_stub.async_playwright = None
    playwright_stub = types.ModuleType("playwright")
    playwright_stub.async_api = playwright_async_api_stub
    sys.modules.setdefault("playwright", playwright_stub)
    sys.modules.setdefault("playwright.async_api", playwright_async_api_stub)

    config_stub = types.ModuleType("app.config")
    config_stub.settings = types.SimpleNamespace(
        LGD_API_BASE="http://115.124.105.220/API",
        REDIS_URL="redis://localhost:6379/0",
    )
    sys.modules["app.config"] = config_stub

    database_stub = types.ModuleType("app.database")

    async def _analyse_boundary_geojson(geojson):
        return {"is_valid": True, "normalized_geojson": geojson, "area_hectares": 1.0}

    database_stub.analyse_boundary_geojson = _analyse_boundary_geojson
    sys.modules["app.database"] = database_stub

    ocr_stub = types.ModuleType("services.ocr_service")
    ocr_stub.extract_text_annotations = lambda *_args, **_kwargs: []
    ocr_stub.preprocess_document_image = lambda image_bytes: image_bytes
    ocr_stub.COORDINATE_TOKEN_RE = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d{2,8}))(?!\d)")
    sys.modules["services.ocr_service"] = ocr_stub

    satellite_stub = types.ModuleType("services.satellite_service")
    satellite_stub.generate_true_color_thumbnail_url = lambda *_args, **_kwargs: None
    sys.modules["services.satellite_service"] = satellite_stub

    try:
        sys.modules.pop("services.land_boundary_service", None)
        return importlib.import_module("services.land_boundary_service")
    finally:
        for name, original_module in original_modules.items():
            if original_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original_module


land_boundary_service = _load_land_boundary_service_module()


def test_lgd_retry_delay_seconds_matches_srs_backoff():
    assert land_boundary_service._lgd_retry_delay_seconds(1) == 2
    assert land_boundary_service._lgd_retry_delay_seconds(2) == 4
    assert land_boundary_service._lgd_retry_delay_seconds(3) == 8