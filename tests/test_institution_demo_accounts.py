"""Offline tests for normal-table demos with private checkpoints and in-process markers."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

from dotenv import dotenv_values
import pytest

from app.config import settings
from app.institution_config import InstitutionSettings, RETIRED_ENV_KEYS
from app.institution_errors import InstitutionError
from demo.institution.blueprints import BLUEPRINTS, phone_for
from demo.institution import sessions
from demo.institution.stages import validate_stage
from scripts.institution.migrate import bundles, sql_bundle


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def store(monkeypatch):
    def make(account):
        user = {
            "id": str(uuid4()),
            "firebase_uid": "offline-demo",
            "phone_number": phone_for(account),
            "account_domain": "INSTITUTION",
        }
        source = SimpleNamespace(
            account_number=account,
            target_user_id=user["id"],
            target_firebase_uid=user["firebase_uid"],
            checkpoint_key=BLUEPRINTS[account]["checkpoint"],
        )
        result = SimpleNamespace(
            user=user,
            source=source,
            state=None,
            restored=[],
            sql=[],
            lock=asyncio.Lock(),
        )

        class Transaction:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class Connection(Transaction):
            def connect(self):
                return self

            def begin(self):
                return self

            async def commit(self):
                pass

            async def rollback(self):
                pass

            async def execute(self, sql, params=None):
                sql = str(sql)
                assert (
                    "demo_checkpoint_registry" not in sql
                    and "demo_login_sessions" not in sql
                )
                result.sql.append(sql)
                if "pg_advisory_lock(" in sql:
                    await result.lock.acquire()
                if "pg_advisory_unlock(" in sql:
                    result.lock.release()

            async def one(self, *args, **kwargs):
                return user

        async def cache(uid, value=None):
            if value is not None:
                result.state = deepcopy(value)
            return deepcopy(result.state)

        async def restore(repo, saved, generation):
            result.restored.append(generation)

        monkeypatch.setitem(
            sys.modules,
            "app.institution_database",
            SimpleNamespace(_require_async_engine=Connection),
        )
        monkeypatch.setattr(sessions, "Repository", lambda conn: conn)
        monkeypatch.setattr(sessions, "require_demo", lambda: None)
        monkeypatch.setattr(
            sessions,
            "account_for",
            lambda uid, phone: source if uid == user["firebase_uid"] else None,
        )
        monkeypatch.setattr(sessions, "recent_auth", lambda claims: None)
        monkeypatch.setattr(sessions, "session_state", cache)
        monkeypatch.setattr(sessions, "_login_locks", {})
        from demo.institution import restore as restore_module

        monkeypatch.setattr(restore_module, "restore_to_checkpoint", restore)
        monkeypatch.setattr(settings, "ENABLE_INSTITUTION_DEMO_ACCOUNTS", True)
        return result

    return make


@pytest.mark.parametrize("account", [5, 6, 7, 8])
def test_new_signin_resets_once_and_token_refresh_preserves_work(store, account):
    repo = store(account)
    claims = {
        "uid": "offline-demo",
        "phone_number": phone_for(account),
        "auth_time": 100,
    }
    first = run(sessions.ensure_login(claims))
    assert first["status"] == "READY" and first["fixture_generation"] == 100
    assert run(sessions.ensure_login({**claims, "iat": 999})) == first
    assert repo.restored == [100]
    second = run(sessions.ensure_login({**claims, "auth_time": 200}))
    assert (
        second["fixture_generation"] == 200
        and second["session_id"] != first["session_id"]
    )
    assert repo.restored == [100, 200]
    with pytest.raises(InstitutionError) as error:
        run(sessions.ensure_login(claims))
    assert error.value.code == "DEMO_SESSION_RESET"


def test_persistent_account_never_calls_restore_or_advances_generation(store):
    repo = store(9)
    for auth_time in (100, 200, 300):
        result = run(
            sessions.ensure_login(
                {
                    "uid": "offline-demo",
                    "phone_number": phone_for(9),
                    "auth_time": auth_time,
                }
            )
        )
        assert result["status"] == "READY" and result["reset_policy"] == "PERSISTENT"
        assert result["reset_performed"] is False and result["fixture_generation"] == 1
    assert repo.restored == []
    assert not any("DELETE" in sql or "UPDATE users" in sql for sql in repo.sql)


@pytest.mark.parametrize("account", [5, 6, 7, 8, 9])
def test_builtin_login_selects_fresh_reset_sample_reset_or_persistent_seed(store, monkeypatch, account):
    from demo.institution import sample_data, restore
    from demo.institution.registry import FreshCheckpoint

    state = store(account)
    source = FreshCheckpoint(state.user["firebase_uid"]) if account == 5 else sample_data.SampleCheckpoint(account, state.user["firebase_uid"])
    monkeypatch.setattr(sessions, "account_for", lambda *args: source)
    built, reset, seeded = [], [], []
    def build(number, uid, user_id):
        built.append(number)
        return sample_data.SampleCheckpoint(number, uid, user_id)
    async def prepare(*args):
        pass
    async def seed(*args):
        seeded.append(account)
    async def replay(repo, saved, generation, **kwargs):
        reset.append((saved.target_user_id, kwargs))
    async def find(*args):
        return [{"id": "persistent-data"}] if seeded else []
    # The fixture's connection object is also its repository.
    engine = sys.modules["app.institution_database"]._require_async_engine
    monkeypatch.setattr(engine, "find", find, raising=False)
    monkeypatch.setattr(sample_data, "build_sample", build)
    monkeypatch.setattr(sample_data, "prepare_sample", prepare)
    monkeypatch.setattr(sample_data, "ensure_sample_globals", prepare)
    monkeypatch.setattr(sample_data, "seed_persistent_once", seed)
    monkeypatch.setattr(restore, "restore_to_checkpoint", replay)
    for when in (100, 200):
        assert run(sessions.ensure_login({"uid": state.user["firebase_uid"], "phone_number": phone_for(account), "auth_time": when}))["status"] == "READY"
    if account == 5:
        assert built == [] and len(reset) == 2
    elif account == 9:
        assert built == [9] and seeded == [9] and reset == []
    else:
        assert built == [account, account]
        assert reset == [(state.user["id"], {"allow_initial_seed": True})] * 2


@pytest.mark.parametrize("deleted", [False, True])
def test_builtin_fresh_login_creates_missing_identity_but_never_revives_deleted_user(store, monkeypatch, deleted):
    from demo.institution.registry import FreshCheckpoint

    state = store(5)
    monkeypatch.setattr(settings, "ENABLE_INSTITUTION_FLOW", True)
    monkeypatch.setattr(sessions, "account_for", lambda *args: FreshCheckpoint(state.user["firebase_uid"]))
    connection = sys.modules["app.institution_database"]._require_async_engine
    original_execute = connection.execute
    created = []
    async def execute(self, sql, params=None):
        await original_execute(self, sql, params)
        if str(sql).startswith("INSERT INTO users"):
            created.append(params)
    async def one(self, sql, params=None, **kwargs):
        if "institution_deleted_uid_hash" in sql:
            return {"id": "deleted"} if deleted else None
        if "firebase_uid=:uid" in sql:
            return state.user if created else None
        return None
    monkeypatch.setattr(connection, "execute", execute)
    monkeypatch.setattr(connection, "one", one)
    claims = {"uid": state.user["firebase_uid"], "phone_number": phone_for(5), "auth_time": 100}
    if deleted:
        with pytest.raises(InstitutionError) as error:
            run(sessions.ensure_login(claims))
        assert error.value.code == "ACCOUNT_DELETED" and not created and not state.restored
    else:
        assert run(sessions.ensure_login(claims))["reset_performed"] is True
        assert len(created) == 1 and state.restored == [100]


def test_concurrent_and_repeated_explicit_handoffs_restore_once(store):
    repo = store(8)
    claims = {"uid": "offline-demo", "phone_number": phone_for(8), "auth_time": 100}
    body = {
        "login_session_id": str(uuid4()),
        "flow": "INSTITUTION",
        "client_audit_demo_mode": True,
    }

    async def simultaneous():
        return await asyncio.gather(
            sessions.start_session(claims, body), sessions.start_session(claims, body)
        )

    responses = run(simultaneous())
    assert (
        json.loads(responses[0].body)["data"] == json.loads(responses[1].body)["data"]
    )
    assert repo.restored == [100]
    with pytest.raises(InstitutionError) as error:
        run(sessions.start_session(claims, {**body, "checkpoint_key": "WRONG"}))
    assert error.value.code == "DEMO_CHECKPOINT_MISMATCH"


def test_normal_users_never_open_demo_database_connection(store):
    repo = store(5)
    assert (
        run(
            sessions.ensure_login(
                {
                    "uid": "normal-user",
                    "phone_number": "+919999999999",
                    "auth_time": 100,
                }
            )
        )
        is None
    )
    assert not repo.sql and not repo.restored


def test_wrong_verified_phone_cannot_start_reset(store):
    repo = store(5)
    with pytest.raises(InstitutionError) as error:
        run(
            sessions.ensure_login(
                {"uid": "offline-demo", "phone_number": phone_for(6), "auth_time": 100}
            )
        )
    assert error.value.code == "ACCOUNT_DOMAIN_MISMATCH" and not repo.sql


def test_persistent_restore_is_refused_before_any_db_or_storage_call(monkeypatch):
    from demo.institution import restore

    monkeypatch.setattr(restore, "require_demo", lambda: None)
    with pytest.raises(InstitutionError) as error:
        run(restore.restore_to_checkpoint(None, SimpleNamespace(account_number=9), 100))
    assert error.value.code == "DEMO_PERSISTENT_ACCOUNT"


def test_failed_restore_can_retry_same_signin_without_exposing_partial_state(
    store, monkeypatch
):
    repo = store(5)
    from demo.institution import restore

    calls = []

    async def attempt(*args):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("Synthetic storage outage")

    monkeypatch.setattr(restore, "restore_to_checkpoint", attempt)
    claims = {"uid": "offline-demo", "phone_number": phone_for(5), "auth_time": 100}
    with pytest.raises(RuntimeError):
        run(sessions.ensure_login(claims))
    assert repo.state["status"] == "RESTORING"
    with pytest.raises(InstitutionError) as error:
        run(sessions.current_state("offline-demo", phone_for(5)))
    assert error.value.code == "DEMO_SESSION_NOT_READY"
    assert run(sessions.ensure_login(claims))["status"] == "READY"
    assert len(calls) == 2 and not repo.lock.locked()


@pytest.mark.parametrize("auth_time", [None, True, "100", 0, -1])
def test_unverified_signin_counter_cannot_trigger_restore(store, auth_time):
    repo = store(5)
    with pytest.raises(InstitutionError) as error:
        run(
            sessions.ensure_login(
                {
                    "uid": "offline-demo",
                    "phone_number": phone_for(5),
                    "auth_time": auth_time,
                }
            )
        )
    assert error.value.code == "RECENT_AUTH_REQUIRED" and not repo.sql


def test_demo_auth_needs_no_special_generation_header_but_rejects_old_signin(store):
    repo = store(5)
    from app.institution_context import Context

    claims = {"uid": "offline-demo", "phone_number": phone_for(5), "auth_time": 100}
    run(sessions.ensure_login(claims))
    request = SimpleNamespace(headers={}, method="POST", state=SimpleNamespace())
    context = Context(request, None, repo.user, claims)
    run(context.check_generation())
    assert context.generation == 100
    run(sessions.ensure_login({**claims, "auth_time": 200}))
    with pytest.raises(InstitutionError) as error:
        run(context.check_generation())
    assert error.value.code == "DEMO_SESSION_RESET"


@pytest.mark.parametrize("allowed", [True, False])
def test_restore_requires_scoped_db_guard_and_uses_only_real_tables(
    monkeypatch, allowed
):
    from demo.institution import restore

    monkeypatch.setattr(restore, "require_demo", lambda: None)
    calls = []

    class Repo:
        @property
        def conn(self):
            return self

        async def execute(self, sql, params=None):
            calls.append((str(sql), params))

        async def rows(self, sql, params=None):
            return []

        async def one(self, sql, params=None):
            return (
                {"allowed": allowed}
                if "tt_demo_restore_allowed" in sql
                else {"total": 0}
            )

    source = SimpleNamespace(
        account_number=5,
        target_user_id=str(uuid4()),
        target_firebase_uid="offline-demo",
        owned_tenant_ids=[],
        objects=[],
        tables=[],
        user_checkpoint=SimpleNamespace(
            onboarding_status="REQUIRED", onboarding_completed_at=None
        ),
    )
    if allowed:
        run(restore.restore_to_checkpoint(Repo(), source, 100))
        assert any(sql.startswith("DELETE") for sql, _ in calls)
    else:
        with pytest.raises(InstitutionError) as error:
            run(restore.restore_to_checkpoint(Repo(), source, 100))
        assert error.value.code == "CHECKPOINT_SCOPE_CHANGED"
        assert not any(sql.startswith("DELETE") for sql, _ in calls)
    assert not any(
        "demo_checkpoint_registry" in sql or "demo_login_sessions" in sql
        for sql, _ in calls
    )
    assert any("terratrust.demo_restore_uid" in sql for sql, _ in calls)


def history():
    tables = {
        "campuses": [
            {
                "id": "campus",
                "control_status": "APPROVED",
                "current_boundary_id": "boundary",
                "current_mask_id": "mask",
                "current_plot_set_id": "plots",
            }
        ],
        "campus_boundaries": [{"id": "boundary", "review_status": "APPROVED"}],
        "campus_vegetation_masks": [
            {
                "id": "mask",
                "review_status": "APPROVED",
                "processing_status": "SUCCEEDED",
            }
        ],
        "campus_plot_sets": [{"id": "plots", "review_status": "APPROVED"}],
        "campus_tree_surveys": [],
        "campus_tree_audits": [],
        "institution_audits": [],
        "institution_calculation_snapshots": [],
        "institution_audit_results": [],
        "institution_exports": [],
        "institution_emission_inputs": [],
        "institution_mitigation_inputs": [],
    }
    for year in (2024, 2025):
        stock, survey, audit, snapshot, result = (
            f"{name}-{year}"
            for name in ("stock", "survey", "audit", "snapshot", "result")
        )
        tables["campus_tree_surveys"].append({"id": survey, "status": "ACCEPTED"})
        tables["campus_tree_audits"].append(
            {
                "id": stock,
                "survey_id": survey,
                "processing_status": "SUCCEEDED",
                "review_status": "ACCEPTED",
                "result_hash": "a" * 64,
                "stock_tC": "10",
                "assessment_date": f"{year}-12-31",
                "comparability_status": "BASELINE_ONLY"
                if year == 2024
                else "COMPARABLE",
                "baseline_audit_id": None if year == 2024 else "stock-2024",
                "signed_change_tCO2": None if year == 2024 else "1",
                "removal_tCO2": None if year == 2024 else "1",
                "release_tCO2": None if year == 2024 else "0",
            }
        )
        tables["institution_audits"].append(
            {
                "id": audit,
                "period_start": f"{year}-01-01",
                "period_end": f"{year}-12-31",
                "business_status": "FINALIZED",
                "reviewed_snapshot_id": snapshot,
                "current_result_id": result,
            }
        )
        tables["institution_calculation_snapshots"].append(
            {
                "id": snapshot,
                "audit_id": audit,
                "result_hash": "a" * 64,
                "frozen_input": {"tree_links": [{"assessment": {"id": stock}}]},
            }
        )
        tables["institution_audit_results"].append(
            {
                "id": result,
                "audit_id": audit,
                "snapshot_id": snapshot,
                "result_hash": "a" * 64,
                "result": {"actual_total": "10"},
            }
        )
        tables["institution_exports"] += [
            {
                "audit_id": audit,
                "snapshot_id": snapshot,
                "result_hash": "a" * 64,
                "status": "SUCCEEDED",
                "draft_watermark": False,
                "format": kind,
            }
            for kind in ("PDF", "CSV", "JSON")
        ]
        for name in ("institution_emission_inputs", "institution_mitigation_inputs"):
            tables[name].append({"audit_id": audit})
    return tables


def test_biomass_stage_rejects_fabricated_annual_removal_and_duplicate_assessments():
    tables = history()
    tables["campus_tree_audits"] = tables["campus_tree_audits"][:1]
    validate_stage(8, tables)
    tables["campus_tree_audits"][0]["removal_tCO2"] = "1"
    with pytest.raises(ValueError):
        validate_stage(8, tables)
    tables["campus_tree_audits"][0]["removal_tCO2"] = None
    tables["campus_tree_audits"] *= 2
    with pytest.raises(ValueError):
        validate_stage(8, tables)


@pytest.mark.parametrize(
    "defect",
    [
        None,
        "missing_2024_pdf",
        "draft_2025",
        "unsupported_removal",
        "wrong_baseline",
        "wrong_year",
    ],
)
def test_two_year_stage_requires_complete_real_history(defect):
    tables = history()
    if defect == "missing_2024_pdf":
        tables["institution_exports"].pop(0)
    elif defect == "draft_2025":
        tables["institution_audits"][1]["business_status"] = "DRAFT"
    elif defect == "unsupported_removal":
        tables["campus_tree_audits"][1]["removal_tCO2"] = None
    elif defect == "wrong_baseline":
        tables["campus_tree_audits"][1]["baseline_audit_id"] = "other"
    elif defect == "wrong_year":
        tables["institution_audits"][0]["period_start"] = "2023-01-01"
    if defect:
        with pytest.raises(ValueError):
            validate_stage(9, tables)
    else:
        validate_stage(9, tables)


def test_fixed_protocol_settings_cannot_be_overridden_by_environment():
    config = InstitutionSettings.model_validate(
        {
            "RAG_TOP_K": 1000,
            "INSTITUTION_UPLOAD_MAX_BYTES": 999999999,
            "LLM_PROVIDER": "OTHER",
        }
    )
    assert (
        config.RAG_TOP_K == 6
        and config.INSTITUTION_UPLOAD_MAX_BYTES == 10485760
        and config.LLM_PROVIDER == "GEMINI"
    )
    values = dotenv_values(Path(__file__).resolve().parents[1] / ".env.example")
    assert not RETIRED_ENV_KEYS & values.keys()
    optional = {"NISAR_ENABLED"}
    assert set(InstitutionSettings.model_fields) - optional <= values.keys()


def test_redundant_deployment_inputs_are_derived_or_retired():
    assert settings.DEPLOYMENT_ENV == settings.ENVIRONMENT
    assert len(settings.INSTITUTION_DEMO_DEPLOYMENT_ID) == 24
    fields = set(InstitutionSettings.model_fields)
    removed = {
        "DEPLOYMENT_ENV",
        "INSTITUTION_DEMO_DEPLOYMENT_ID",
        "INSTITUTION_COMMISSIONING_UID",
        "INSTITUTION_DEMO_DATABASE_URL",
        "BIOMASS_MODEL_BACKEND",
    }
    assert not fields & removed
    assert removed <= RETIRED_ENV_KEYS


def test_registered_legacy_manifest_survives_retired_deployment_knobs(
    monkeypatch, tmp_path
):
    from demo.institution import registry
    from models.institution.common import digest

    monkeypatch.setattr(registry, "require_demo", lambda: None)
    identifier = lambda: str(uuid4())
    data = {
        "schema_version": 1,
        "manifest_id": identifier(),
        "manifest_version": 1,
        "checkpoint_key": "INSTITUTION_FRESH",
        "account_number": 5,
        "target_firebase_uid": "offline-legacy-uid",
        "target_user_id": identifier(),
        "deployment_id": "OLD_MANUALLY_CHOSEN_ID",
        "deployment_env": "test",
        "owned_tenant_ids": [],
        "created_at": "2026-10-21T00:00:00Z",
        "created_by": identifier(),
        "prerequisites": {
            "method_ids": [],
            "factor_ids": [],
            "model_ids": [],
            "quality_policy_ids": [],
            "corpus_ids": [],
            "config_hash": "a" * 64,
        },
        "user_checkpoint": {
            "onboarding_status": "REQUIRED",
            "onboarding_completed_at": None,
            "registration_id": None,
            "registration_institution_id": None,
        },
        "tables": [],
        "objects": [],
        "expected": {
            "landing_screen": "Institution Setup",
            "permission": None,
            "tenant_count": 0,
            "campus_count": 0,
            "survey_count": 0,
            "tree_audit_count": 0,
            "audit_count": 0,
            "current_final_audit_id": None,
            "result_hash": None,
            "export_ids": [],
            "published_query_ids": [],
        },
        "provenance": {
            "prepared_via_normal_services": True,
            "review_case_ids": [],
            "source_record_ids": [],
            "validation_record_id": identifier(),
        },
    }
    data["manifest_sha256"] = digest(data)
    stored = {
        "firebase_uid": data["target_firebase_uid"],
        "account_number": 5,
        "checkpoint_key": "INSTITUTION_FRESH",
        "reset_policy": "NEXT_LOGIN",
        "source_manifest": data,
        "source_manifest_hash": data["manifest_sha256"],
    }
    assert registry.manifest(stored).deployment_id == "OLD_MANUALLY_CHOSEN_ID"
    monkeypatch.setattr(registry, "DIRECTORY", tmp_path / "private-demo")
    registry.save_checkpoint(registry.manifest(stored))
    saved = registry.account_for(data["target_firebase_uid"], phone_for(5))
    assert saved.manifest_sha256 == data["manifest_sha256"]
    with pytest.raises(InstitutionError, match="different Firebase identity"):
        registry.account_for("another-user", phone_for(5))
    assert registry.account_for(data["target_firebase_uid"], phone_for(6)).account_number == 6
    before = (registry.DIRECTORY / "account-5.json").read_bytes()
    other = {**data, "target_firebase_uid": "another-user"}
    other["manifest_sha256"] = digest(
        {k: v for k, v in other.items() if k != "manifest_sha256"}
    )
    from demo.institution.manifest import CheckpointManifest

    with pytest.raises(ValueError, match="rebound"):
        registry.save_checkpoint(CheckpointManifest.model_validate(other))
    assert (registry.DIRECTORY / "account-5.json").read_bytes() == before
    for name, value in (
        ("firebase_uid", "other-uid"),
        ("source_manifest_hash", "b" * 64),
        ("reset_policy", "PERSISTENT"),
    ):
        with pytest.raises(InstitutionError):
            registry.manifest({**stored, name: value})


def test_sql_editor_bundle_preserves_exact_migration_ledger_and_private_tables():
    parse_sql = pytest.importorskip("pglast").parse_sql
    source = sql_bundle()
    assert parse_sql(source) and source.count("BEGIN;") == 1
    for version, paths, checksum in bundles():
        assert checksum in source
        for path in paths:
            assert path.read_text(encoding="utf-8") in source
    import re

    created = {
        name
        for _, paths, _ in bundles()
        for path in paths
        for name in re.findall(
            r"CREATE TABLE (\w+)\s*\(", path.read_text(encoding="utf-8")
        )
    }
    acl = (
        Path(__file__).resolve().parents[1]
        / "migrations/institution/013_supabase_private_access.sql"
    ).read_text()
    forward = (
        Path(__file__).resolve().parents[1]
        / "migrations/institution/015_simple_configuration.sql"
    ).read_text()
    assert all(
        "'" + table + "'" in acl
        or (
            "ALTER TABLE " + table + " ENABLE ROW LEVEL SECURITY" in forward
            and table in forward.split("REVOKE ALL ON", 1)[1]
        )
        for table in created
    )
    forward = (
        Path(__file__).resolve().parents[1]
        / "migrations/institution/014_same_database_demo_restore.sql"
    ).read_text()
    assert parse_sql(forward)
    assert "CREATE ROLE" not in forward and "session_user=" not in forward
    assert (
        "s.id=r.last_session_id" in forward
        and "s.generation=r.current_generation" in forward
    )
    assert "account_number BETWEEN 5 AND 8" in forward
    assert "unnest(registered.owned_tenant_ids)" in forward
    import re
    from pglast import parse_plpgsql

    functions = re.findall(
        r"CREATE (?:OR REPLACE )?FUNCTION .*?(?:END \$scope_guard\$;|END \$\$;)",
        forward,
        re.S,
    )
    assert len(functions) == 5
    for function in functions:
        parse_plpgsql(function)


def test_forward_demo_cleanup_drops_only_control_tables_and_keeps_ledger_checks():
    parser = pytest.importorskip("pglast")
    from pglast import ast
    from pglast.enums import DropBehavior

    path = (
        Path(__file__).resolve().parents[1]
        / "migrations/institution/016_file_based_demo_checkpoints.sql"
    )
    source = path.read_text(encoding="utf-8")
    statements = [item.stmt for item in parser.parse_sql(source)]
    drops = [item for item in statements if isinstance(item, ast.DropStmt)]
    assert {item.objects[0][-1].sval for item in drops} == {
        "demo_login_sessions",
        "demo_checkpoint_registry",
    }
    assert all(item.behavior == DropBehavior.DROP_RESTRICT for item in drops)
    assert not any(
        isinstance(item, (ast.DeleteStmt, ast.UpdateStmt, ast.TruncateStmt))
        for item in statements
    )
    parser.parse_plpgsql(source.split("REVOKE ALL", 1)[0])
    assert "+919000000009" not in source and "account_domain='INSTITUTION'" in source
    assert "i.created_by=target.id" in source and "m.user_id<>target.id" in source
    upgrade = sql_bundle(min_version=16)
    parser.parse_sql(upgrade)
    assert "Required migration 015" in upgrade
    assert "CREATE TABLE demo_checkpoint_registry" not in upgrade
    assert "CREATE TABLE demo_login_sessions" not in upgrade


def test_restore_refuses_tenants_shared_with_any_other_member(monkeypatch):
    from demo.institution import restore

    monkeypatch.setattr(restore, "require_demo", lambda: None)

    class Repo:
        async def rows(self, sql, params=None):
            return (
                [{"id": "tenant"}]
                if "FROM institutions" in sql
                else [{"user_id": "another-user"}]
            )

    source = SimpleNamespace(
        account_number=5, target_user_id="demo-user", owned_tenant_ids=[]
    )
    with pytest.raises(InstitutionError) as error:
        run(restore.restore_to_checkpoint(Repo(), source, 100))
    assert error.value.code == "CHECKPOINT_SCOPE_CHANGED"


def test_denied_database_scope_never_copies_checkpoint_files(monkeypatch):
    from demo.institution import restore

    monkeypatch.setattr(restore, "require_demo", lambda: None)

    class Repo:
        @property
        def conn(self):
            return self

        async def rows(self, *args):
            return []

        async def execute(self, *args):
            pass

        async def one(self, *args):
            return {"allowed": False}

    # This deliberately incomplete object would fail if storage were touched.
    source = SimpleNamespace(
        account_number=5,
        target_user_id="demo-user",
        target_firebase_uid="demo-uid",
        owned_tenant_ids=[],
        objects=[object()],
    )
    with pytest.raises(InstitutionError) as error:
        run(restore.restore_to_checkpoint(Repo(), source, 100))
    assert error.value.code == "CHECKPOINT_SCOPE_CHANGED"


@pytest.mark.parametrize("current_generation", [None, 200, 100])
def test_separate_worker_checks_normal_job_rows_without_api_session_memory(
    monkeypatch, current_generation
):
    from datetime import datetime, timedelta, timezone
    from tasks.institution_tasks import guarded

    monkeypatch.setattr(settings, "ENABLE_INSTITUTION_DEMO_ACCOUNTS", True)
    monkeypatch.setattr(sessions, "_login_states", {})
    actor = {"id": "actor", "firebase_uid": "uid", "phone_number": phone_for(5)}

    class Repo:
        async def one(self, *args, **kwargs):
            return actor

        async def get(self, table, *args, **kwargs):
            if table == "processing_jobs":
                return (
                    None
                    if current_generation is None
                    else {"fixture_generation": current_generation}
                )
            return {"input_hash": "same", "input_revision": 1}

    job = {
        "id": "job",
        "requested_by": "actor",
        "deadline_at": datetime.now(timezone.utc) + timedelta(minutes=1),
        "fixture_generation": 100,
        "job_type": "institution_calculation",
        "institution_id": None,
        "resource_id": "audit",
        "input_hash": "same",
        "source_input_revision": 1,
    }
    if current_generation == 100:
        assert run(guarded(Repo(), job, lock=True))[0] == actor
    else:
        with pytest.raises(InstitutionError) as error:
            run(guarded(Repo(), job, lock=True))
        assert error.value.code == "DEMO_SESSION_RESET"
