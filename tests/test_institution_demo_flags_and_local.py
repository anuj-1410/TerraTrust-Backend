"""Independent demo controls and local process/SQL deployment boundaries."""

import subprocess
import sys

import pytest

from app.config import Settings, settings
from app.institution_config import InstitutionSettings
from scripts.institution.migrate import bundles, preflight_sql, sql_bundle
from scripts.run_local import commands


@pytest.mark.parametrize(
    "farmer,institution", [(False, False), (False, True), (True, False), (True, True)]
)
def test_demo_switches_are_independent(farmer, institution):
    data = settings.model_dump()
    data.update(
        ENVIRONMENT="development",
        ENABLE_FARMER_DEMO_ACCOUNTS=farmer,
        ENABLE_INSTITUTION_DEMO_ACCOUNTS=institution,
    )
    configured = Settings(_env_file=None, **data)
    assert configured.ENABLE_FARMER_DEMO_ACCOUNTS is farmer
    assert configured.ENABLE_INSTITUTION_DEMO_ACCOUNTS is institution


def test_old_ambiguous_flag_cannot_enable_institution_demos():
    configured = InstitutionSettings.model_validate({"AUDIT_DEMO_MODE": True})
    assert configured.ENABLE_INSTITUTION_DEMO_ACCOUNTS is False


def test_farmer_legacy_alias_and_explicit_override(tmp_path):
    data = settings.model_dump()
    data.update(ENVIRONMENT="development", ENABLE_INSTITUTION_DEMO_ACCOUNTS=False)
    data.pop("ENABLE_FARMER_DEMO_ACCOUNTS")
    path = tmp_path / "demo.env"
    path.write_text("ENABLE_DEMO_ACCOUNTS=true\n")
    assert Settings(_env_file=path, **data).ENABLE_FARMER_DEMO_ACCOUNTS is True
    path.write_text("ENABLE_DEMO_ACCOUNTS=true\nENABLE_FARMER_DEMO_ACCOUNTS=false\n")
    assert Settings(_env_file=path, **data).ENABLE_FARMER_DEMO_ACCOUNTS is False


def test_native_farmer_switch_ignores_institution_switch():
    code = """
import importlib,os
os.environ.update(ENABLE_FARMER_DEMO_ACCOUNTS='false',ENABLE_INSTITUTION_DEMO_ACCOUNTS='true',ENABLE_DEMO_ACCOUNTS='true')
from demo import config
assert not config.ENABLE_DEMO_ACCOUNTS
os.environ['ENABLE_FARMER_DEMO_ACCOUNTS']='true'
os.environ['ENABLE_INSTITUTION_DEMO_ACCOUNTS']='false'
importlib.reload(config)
assert config.ENABLE_DEMO_ACCOUNTS
"""
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True)


def test_windows_local_commands_use_solo_and_keep_farmer_worker_opt_in():
    selected = commands("python.exe")
    assert "farmer" not in selected
    assert all(
        "--pool=solo" in selected[name] for name in ("operations", "institution")
    )
    assert "127.0.0.1" in selected["api"] and "8000" in selected["api"]
    assert set(commands("python.exe", institution=False)) == {"api"}
    assert "farmer" in commands("python.exe", farmer=True)
    assert "--reload" not in selected["api"]


def test_actual_institution_solo_worker_boots_without_network():
    code = """
import socket
def denied(*args, **kwargs):
    raise AssertionError('Offline worker cannot open network sockets.')
socket.socket.connect = denied
socket.socket.connect_ex = denied
from celery import signals
from celery.exceptions import WorkerShutdown
from tasks.institution_app import institution_app
institution_app.conf.update(broker_url='memory://', result_backend=None, broker_transport_options={}, worker_enable_remote_control=False)
@signals.worker_ready.connect(weak=False)
def ready(**kwargs):
    print('OFFLINE_WINDOWS_SOLO_READY', flush=True)
    raise WorkerShutdown(0)
worker=institution_app.Worker(pool='solo', concurrency=1, without_gossip=True, without_mingle=True, without_heartbeat=True, queues=['tt_offline_check'], loglevel='WARNING')
worker.start()
"""
    outcome = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "OFFLINE_WINDOWS_SOLO_READY" in outcome.stdout + outcome.stderr


def test_supabase_bundle_preflight_and_farmer_ddl_scope():
    parser = pytest.importorskip("pglast")
    from pglast import ast

    created = {
        name
        for _, files, _ in bundles()
        for path in files
        for item in parser.parse_sql(path.read_text())
        if isinstance(item.stmt, ast.CreateStmt)
        for name in [item.stmt.relation.relname]
    }
    for _, files, _ in bundles():
        for path in files:
            for item in parser.parse_sql(path.read_text()):
                node = item.stmt
                if isinstance(node, ast.AlterTableStmt):
                    assert node.relation.relname in created | {"users"}
                assert not isinstance(node, (ast.DeleteStmt, ast.TruncateStmt))
    preflight = preflight_sql()
    parser.parse_sql(preflight)
    parser.parse_plpgsql(preflight)
    assert "Unexpected table overlaps" in preflight
    bundle = sql_bundle()
    assert bundle.index(preflight) < bundle.index("DO $phase_guard$")
    assert "BEGIN;" in bundle and "COMMIT;" in bundle
