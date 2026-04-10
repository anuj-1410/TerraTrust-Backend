import importlib
import sys
import types


def _load_fusion_task_module():
    celery_module = types.ModuleType("tasks.celery_app")

    class _CeleryAppStub:
        @staticmethod
        def task(*_args, **_kwargs):
            def decorator(func):
                return func

            return decorator

    celery_module.celery_app = _CeleryAppStub()
    sys.modules["tasks.celery_app"] = celery_module

    database_module = types.ModuleType("app.database")
    database_module.fetch_land_parcel_record = lambda *_args, **_kwargs: None
    database_module.list_sampling_zones_for_audit = lambda *_args, **_kwargs: None
    database_module.list_tree_scans_for_audit = lambda *_args, **_kwargs: None
    database_module.update_tree_scan_measurements = lambda *_args, **_kwargs: None
    database_module.supabase_client = None
    sys.modules["app.database"] = database_module

    fusion_engine_module = types.ModuleType("services.fusion_engine")
    sys.modules["services.fusion_engine"] = fusion_engine_module

    sys.modules.pop("tasks.fusion_task", None)
    return importlib.import_module("tasks.fusion_task")


fusion_task = _load_fusion_task_module()


def test_fusion_retry_delay_seconds_matches_srs_schedule():
    assert fusion_task._fusion_retry_delay_seconds(0) == 300
    assert fusion_task._fusion_retry_delay_seconds(1) == 1800
    assert fusion_task._fusion_retry_delay_seconds(5) == 1800