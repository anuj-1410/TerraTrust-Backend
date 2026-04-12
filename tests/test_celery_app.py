import importlib
import sys
import types


def _load_celery_module(redis_url: str):
    created = {}

    celery_stub = types.ModuleType("celery")

    class _CeleryStub:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.updated_config = {}
            self.conf = types.SimpleNamespace(update=self.updated_config.update)
            created["app"] = self

    celery_stub.Celery = _CeleryStub
    sys.modules["celery"] = celery_stub

    config_stub = types.ModuleType("app.config")
    config_stub.settings = types.SimpleNamespace(REDIS_URL=redis_url)
    sys.modules["app.config"] = config_stub

    sys.modules.pop("tasks.celery_app", None)
    module = importlib.import_module("tasks.celery_app")
    return module, created["app"]


def test_celery_uses_tls_verified_redis_urls_for_broker_and_backend():
    _module, celery_app = _load_celery_module(
        "rediss://default:secret@example.upstash.io:6379/0?ssl_cert_reqs=CERT_NONE"
    )

    assert celery_app.kwargs["broker"] == (
        "rediss://default:secret@example.upstash.io:6379/0?ssl_cert_reqs=required"
    )
    assert celery_app.kwargs["backend"] == (
        "rediss://default:secret@example.upstash.io:6379/0?ssl_cert_reqs=required"
    )