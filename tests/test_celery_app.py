import importlib
import sys
import types


def _load_celery_module(redis_url: str):
    created = {}
    missing = object()
    previous_celery = sys.modules.get("celery", missing)
    previous_config = sys.modules.get("app.config", missing)

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
    try:
        module = importlib.import_module("tasks.celery_app")
    finally:
        if previous_celery is missing:
            sys.modules.pop("celery", None)
        else:
            sys.modules["celery"] = previous_celery

        if previous_config is missing:
            sys.modules.pop("app.config", None)
        else:
            sys.modules["app.config"] = previous_config

    return module, created["app"]


def test_celery_uses_tls_verified_redis_broker_without_result_backend():
    _module, celery_app = _load_celery_module(
        "rediss://default:secret@example.upstash.io:6379/0?ssl_cert_reqs=CERT_NONE"
    )

    assert celery_app.kwargs["broker"] == (
        "rediss://default:secret@example.upstash.io:6379/0?ssl_cert_reqs=required"
    )
    assert "backend" not in celery_app.kwargs
    assert celery_app.updated_config["task_ignore_result"] is True
