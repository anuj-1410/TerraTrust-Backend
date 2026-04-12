import importlib.util
import sys
import types
from pathlib import Path


def _load_satellite_module(create_signed_url_response):
    ee_stub = types.ModuleType("ee")
    ee_stub.Geometry = type("Geometry", (), {})
    ee_stub.Image = type("Image", (), {})
    sys.modules["ee"] = ee_stub

    httpx_stub = types.ModuleType("httpx")

    class _Response:
        def __init__(self):
            self.content = b"png"

        def raise_for_status(self):
            return None

    httpx_stub.get = lambda *_args, **_kwargs: _Response()
    sys.modules["httpx"] = httpx_stub

    database_stub = types.ModuleType("app.database")

    class _Bucket:
        def __init__(self):
            self.upload_calls = 0

        def create_signed_url(self, *_args, **_kwargs):
            response = create_signed_url_response
            if isinstance(response, Exception):
                raise response
            return response

        def upload(self, *_args, **_kwargs):
            self.upload_calls += 1

    bucket = _Bucket()

    class _Storage:
        def from_(self, _bucket_name):
            return bucket

    database_stub.supabase_client = types.SimpleNamespace(storage=_Storage())
    sys.modules["app.database"] = database_stub

    gee_stub = types.ModuleType("app.gee")
    gee_stub.ensure_gee_initialized = lambda: None
    sys.modules["app.gee"] = gee_stub

    module_path = Path(__file__).resolve().parents[1] / "services" / "satellite_service.py"
    spec = importlib.util.spec_from_file_location("_test_satellite_service", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop("_test_satellite_service", None)
    spec.loader.exec_module(module)
    return module, bucket


def test_existing_thumbnail_skips_reupload_when_signed_url_already_exists():
    satellite_service, bucket = _load_satellite_module(
        {"signedURL": "https://example.com/thumb.png"}
    )

    signed_url = satellite_service._get_existing_thumbnail_signed_url("thumbnails/demo.png")

    assert signed_url == "https://example.com/thumb.png"
    assert bucket.upload_calls == 0


def test_missing_thumbnail_returns_none_without_raising():
    satellite_service, bucket = _load_satellite_module(RuntimeError("Object not found"))

    signed_url = satellite_service._get_existing_thumbnail_signed_url("thumbnails/demo.png")

    assert signed_url is None
    assert bucket.upload_calls == 0