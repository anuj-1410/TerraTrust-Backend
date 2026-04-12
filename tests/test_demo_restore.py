import importlib
import sys
import types
from datetime import datetime, timezone


def _load_restore_module():
    sqlalchemy_stub = types.ModuleType("sqlalchemy")
    sqlalchemy_stub.text = lambda query: query
    sys.modules["sqlalchemy"] = sqlalchemy_stub

    database_stub = types.ModuleType("app.database")
    database_stub._require_async_engine = lambda: None
    database_stub.supabase_client = None
    sys.modules["app.database"] = database_stub

    config_stub = types.ModuleType("demo.config")
    config_stub.get_demo_account = lambda _uid: None
    config_stub.is_resettable_demo = lambda _uid: True
    sys.modules["demo.config"] = config_stub

    checkpoints_stub = types.ModuleType("demo.checkpoints")
    checkpoints_stub.CHECKPOINT_BUILDERS = {}
    sys.modules["demo.checkpoints"] = checkpoints_stub

    sys.modules.pop("demo.restore", None)
    return importlib.import_module("demo.restore")


def test_coerce_checkpoint_timestamp_parses_iso_zulu_string():
    restore = _load_restore_module()

    value = restore._coerce_checkpoint_timestamp("2024-11-15T10:00:00Z")

    assert value == datetime(2024, 11, 15, 10, 0, tzinfo=timezone.utc)


def test_normalise_checkpoint_audit_converts_minted_at_for_asyncpg():
    restore = _load_restore_module()

    audit = restore._normalise_checkpoint_audit(
        {
            "status": "MINTED",
            "minted_at": "2024-11-15T10:00:00Z",
        }
    )

    assert audit["status"] == "MINTED"
    assert audit["minted_at"] == datetime(2024, 11, 15, 10, 0, tzinfo=timezone.utc)
