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
    sys.modules["demo.config"] = config_stub

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


def test_manual_demo_reset_allows_persistent_account_four():
    demo_router = _load_demo_router_module()

    response = asyncio.run(demo_router.manually_reset_demo_account("9000000004"))

    assert response["status"] == "reset_complete"
    assert response["phone"] == "+919000000004"
    assert response["checkpoint"] == "FULL"
    assert response["persistent"] is True