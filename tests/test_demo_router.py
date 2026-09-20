import asyncio
import importlib
import sys
import types


def _install_fastapi_stub():
    fastapi_stub = types.ModuleType("fastapi")

    class _APIRouter:
        def __init__(self, *_args, **_kwargs):
            pass

        def post(self, *_args, **_kwargs):
            def decorator(func):
                return func

            return decorator

        def get(self, *_args, **_kwargs):
            def decorator(func):
                return func

            return decorator

    fastapi_stub.APIRouter = _APIRouter
    fastapi_stub.Depends = lambda dependency=None: dependency

    class _HTTPException(Exception):
        def __init__(self, status_code, detail, headers=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail
            self.headers = headers or {}

    fastapi_stub.HTTPException = _HTTPException
    fastapi_stub.status = types.SimpleNamespace(
        HTTP_403_FORBIDDEN=403,
        HTTP_409_CONFLICT=409,
    )
    sys.modules["fastapi"] = fastapi_stub


def _load_demo_router_module():
    _install_fastapi_stub()

    config_stub = types.ModuleType("demo.config")
    config_stub.DEMO_FIREBASE_UIDS = {
        "+919000000004": "KhBSyGEVU8SkMWIXmN8qkrLNwYk1",
    }
    config_stub.DEMO_UID_PLACEHOLDER = "PASTE_UID_HERE"
    config_stub.ENABLE_DEMO_ACCOUNTS = True
    config_stub.get_demo_account = lambda _uid: {"checkpoint": "FULL", "persistent": True}
    config_stub.get_demo_status_accounts = lambda: []
    config_stub.is_demo_uid = lambda uid: uid == "KhBSyGEVU8SkMWIXmN8qkrLNwYk1"
    config_stub.is_resettable_demo = lambda _uid: False
    sys.modules["demo.config"] = config_stub

    dependencies_stub = types.ModuleType("app.dependencies")
    dependencies_stub.get_current_user = lambda: None
    sys.modules["app.dependencies"] = dependencies_stub

    middleware_stub = types.ModuleType("demo.middleware")
    middleware_stub.invalidate_demo_session = lambda _uid: None
    sys.modules["demo.middleware"] = middleware_stub

    restore_stub = types.ModuleType("demo.restore")

    async def restore_to_checkpoint(_uid, allow_persistent=False):
        return allow_persistent

    restore_stub.restore_to_checkpoint = restore_to_checkpoint
    sys.modules["demo.restore"] = restore_stub

    sys.modules.pop("demo.router", None)
    return importlib.import_module("demo.router")


def test_manual_demo_reset_rejects_persistent_account_four():
    demo_router = _load_demo_router_module()

    try:
        asyncio.run(
            demo_router.manually_reset_demo_account(
                "9000000004",
                current_user={"firebase_uid": "KhBSyGEVU8SkMWIXmN8qkrLNwYk1"},
            )
        )
    except Exception as exc:
        assert exc.status_code == 409
        assert exc.detail == "This demo account is persistent and cannot be reset."
    else:
        raise AssertionError("Expected persistent demo reset to be rejected.")
