"""Offline contracts for automatic, labelled samples in normal business tables."""

import asyncio
import re
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
from shapely.geometry import shape

from app.institution_config import InstitutionSettings
from app.institution_errors import InstitutionError
from demo.institution import registry
from demo.institution.blueprints import phone_for
from demo.institution.sample_data import build_sample, seed_persistent_once
from demo.institution.stages import validate_stage
from models.institution.common import digest, measurement_hash, normalize
from repositories.institution.base import Repository, columns

USER_ID = "d88a4ab6-1c33-423c-a589-7ac7ad36b94e"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def samples():
    return {n: build_sample(n, f"offline-sample-{n}", USER_ID) for n in range(6, 10)}


@pytest.mark.parametrize("environment", ["production", "development", "test"])
def test_blank_public_pages_are_optional_in_all_environments(environment):
    class Config(InstitutionSettings):
        ENVIRONMENT: str = environment

    config = Config(ENABLE_INSTITUTION_FLOW=True)
    assert config.institution_missing_config() == []
    config.INSTITUTION_SUPPORT_URL = "http://example.invalid"
    assert config.institution_missing_config() == ["INSTITUTION_SUPPORT_URL"]
    config.INSTITUTION_SUPPORT_URL = "https://example.invalid/support"
    assert config.institution_missing_config() == []
    assert "GEMINI_API_KEY" in config.institution_missing_config(advisor=True)


def test_five_builtin_stages_need_no_private_files_and_ignore_normal_phones(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(registry, "DIRECTORY", tmp_path)
    monkeypatch.setattr(registry, "SECRET_DIRECTORY", tmp_path / "secrets")
    for account in range(5, 10):
        source = registry.account_for("verified-firebase-uid", phone_for(account))
        assert source.account_number == account
        assert source.target_firebase_uid == "verified-firebase-uid"
    assert registry.account_for("normal-user", "+919876543210") is None
    assert registry.account_for("normal-user") is None


@pytest.mark.parametrize("account", range(6, 10))
def test_stage_counts_geometry_hashes_and_persisted_types(samples, account):
    sample = samples[account]
    data = {bundle.table: bundle.rows for bundle in sample.tables}
    validate_stage(account, data)
    assert len(data.get("campuses", [])) == (0 if account == 6 else 1)
    assert len(data.get("campus_tree_measurements", [])) == (
        24 if account == 9 else 12 if account == 8 else 0
    )
    assert all(
        value["data_origin"] == "SYNTHETIC" for rows in data.values() for value in rows
    )
    repo = Repository(None)
    for table, rows in [
        *data.items(),
        *[(table, [value]) for table, value in sample.globals],
    ]:
        for value in rows:
            assert set(value) <= columns(table)
            # The actual repository's typed bindings must round-trip identically.
            _, params = repo.binds(table, value)
            for key, bound in params.items():
                if isinstance(bound, Decimal) or key in {
                    "created_at",
                    "updated_at",
                    "joined_at",
                    "measured_at",
                    "validated_at",
                }:
                    assert normalize(bound) == value[key]
    for bundle in sample.tables:
        assert bundle.row_hashes == [digest(value) for value in bundle.rows]
    for measurement in data.get("campus_tree_measurements", []):
        assert measurement_hash(measurement["raw_input"]) == measurement["content_hash"]
        plot = next(
            p
            for p in data["campus_sampling_plots"]
            if p["id"] == measurement["plot_id"]
        )
        assert shape(plot["geometry"]).covers(shape(measurement["location"]))
    for stock in data.get("campus_tree_audits", [])[:1]:
        assert stock["removal_tCO2"] is None and stock["release_tCO2"] is None


def test_samples_satisfy_migration_required_columns_and_foreign_keys(samples):
    from pglast import ast, enums, parse_sql

    required = defaultdict(set)
    foreign = []
    for path in sorted((ROOT / "migrations/institution").glob("*.sql")):
        source = path.read_text(encoding="utf-8")
        for statement in parse_sql(source):
            stmt = statement.stmt
            if isinstance(stmt, ast.CreateStmt):
                for column in stmt.tableElts or ():
                    if isinstance(column, ast.ColumnDef):
                        kinds = {c.contype for c in column.constraints or ()}
                        if (
                            enums.ConstrType.CONSTR_NOTNULL in kinds
                            and enums.ConstrType.CONSTR_DEFAULT not in kinds
                        ):
                            required[stmt.relation.relname].add(column.colname)
        foreign += re.findall(
            r"ALTER TABLE (\w+) ADD CONSTRAINT \w+ FOREIGN KEY\s*\(([^)]+)\) REFERENCES (\w+)\(([^)]+)\)",
            source,
        )
    for sample in samples.values():
        all_rows = defaultdict(list)
        all_rows["users"] = [{"id": USER_ID}]
        for table, value in sample.globals:
            all_rows[table].append(value)
        for bundle in sample.tables:
            all_rows[bundle.table].extend(bundle.rows)
        for table, rows in all_rows.items():
            for value in rows:
                assert all(value.get(key) is not None for key in required[table]), (
                    table,
                    required[table] - value.keys(),
                )
        for table, local_keys, parent, parent_keys in foreign:
            keys = [k.strip() for k in local_keys.split(",")]
            target_keys = [k.strip() for k in parent_keys.split(",")]
            for value in all_rows[table]:
                local = tuple(value.get(k) for k in keys)
                if None not in local:
                    assert any(
                        local == tuple(target.get(k) for k in target_keys)
                        for target in all_rows[parent]
                    ), (table, keys, parent, local)


def test_history_uses_real_accounting_and_real_export_bytes(samples):
    from services.institution.accounting_service import calculate_inventory

    sample = samples[9]
    data = {bundle.table: bundle.rows for bundle in sample.tables}
    for snapshot in data["institution_calculation_snapshots"]:
        expected, issues = calculate_inventory(
            snapshot["frozen_input"], snapshot_id=snapshot["id"]
        )
        assert issues == []
        result = snapshot["result"]
        assert expected["result_hash"] == result["result_hash"]
        assert Decimal(result["actual_total"]) == Decimal("92.094")
        assert Decimal(result["quantified_avoided_total"]) == Decimal("10.65")
    assert len(sample.objects) == 6
    for obj in sample.objects:
        assert obj.size_bytes > 0
        if obj.mime_type == "application/pdf":
            assert obj.data.startswith(b"%PDF-")
        else:
            assert b"SYNTHETIC" in obj.data


def test_sample_profiles_inputs_and_results_match_mobile_wire_contracts(samples):
    from models.institution.contracts import MODELS
    from services.institution.annual_service import line_public
    from services.institution.base import public
    from services.institution.survey_service import tree_audit_resource

    data = {bundle.table: bundle.rows for bundle in samples[9].tables}
    MODELS["ProfileData"].model_validate(data["institutions"][0]["profile"])
    for row in data["campus_tree_measurements"]:
        MODELS["MeasurementItem"].model_validate(row["raw_input"])
    for table, name in [
        ("institution_emission_inputs", "EmissionLine"),
        ("institution_mitigation_inputs", "MitigationLine"),
    ]:
        for row in data[table]:
            MODELS[name].model_validate(line_public(table, row))
    for row in data["campus_tree_audits"]:
        MODELS["TreeAuditResource"].model_validate(tree_audit_resource(row))
    for row in data["institution_audit_results"]:
        MODELS["ResultResource"].model_validate(
            public("ResultResource", {**row, **row["result"]})
        )


class MemoryRepo:
    def __init__(self, existing=False):
        self.conn = self
        self.sql = []
        self.values = defaultdict(dict)
        self.existing = existing
        self.insert_states = []

    async def execute(self, sql, params=None):
        self.sql.append(str(sql))

    async def find(self, table, filters):
        return (
            [{"id": "preserved"}] if self.existing and table == "institutions" else []
        )

    async def get(self, table, rid, **kwargs):
        return self.values[table].get(rid)

    async def insert(self, table, value):
        if table in {
            "campus_tree_measurements",
            "institution_emission_inputs",
            "institution_mitigation_inputs",
            "institution_audit_tree_links",
        }:
            parent, fk, status = (
                ("campus_tree_surveys", "survey_id", "status")
                if table == "campus_tree_measurements"
                else ("institution_audits", "audit_id", "business_status")
            )
            assert self.values[parent][value[fk]][status] == "DRAFT"
        self.values[table][value["id"]] = deepcopy(value)
        self.insert_states.append((table, deepcopy(value)))
        return value

    async def update(self, table, rid, value):
        self.values[table][rid].update(value)


def test_persistent_seed_stages_inputs_without_reset_and_preserves_next_login(
    samples, monkeypatch
):
    from services.shared import evidence_service

    uploaded = []

    async def write(*args):
        uploaded.append(args)

    monkeypatch.setattr(evidence_service, "write_bytes", write)
    repo = MemoryRepo()
    assert asyncio.run(seed_persistent_once(repo, samples[9])) is True
    assert len(uploaded) == 6
    assert not any("DELETE" in sql or "demo_restore" in sql for sql in repo.sql)
    assert all(
        v["business_status"] == "FINALIZED"
        for v in repo.values["institution_audits"].values()
    )
    repo.existing = True
    before = deepcopy(repo.values)
    assert asyncio.run(seed_persistent_once(repo, samples[9])) is False
    assert repo.values == before and len(uploaded) == 6


def test_initial_reset_cannot_overwrite_an_existing_unrelated_institution(
    samples, monkeypatch
):
    from demo.institution import restore

    monkeypatch.setattr(restore, "require_demo", lambda: None)

    class ConflictingRepo(MemoryRepo):
        async def rows(self, *args):
            return []

        async def get(self, table, rid, **kwargs):
            return {"id": rid, "created_by": "another-user"}

    repo = ConflictingRepo()
    with pytest.raises(InstitutionError) as error:
        asyncio.run(
            restore.restore_to_checkpoint(
                repo, samples[6], 100, allow_initial_seed=True
            )
        )
    assert error.value.code == "CHECKPOINT_SCOPE_CHANGED" and repo.sql == []


@pytest.mark.parametrize("account", [6, 7, 8])
def test_initial_sample_restore_replays_parent_first_and_next_login_resets(
    samples, monkeypatch, account
):
    from demo.institution import restore

    monkeypatch.setattr(restore, "require_demo", lambda: None)

    class ResetRepo(MemoryRepo):
        async def rows(self, sql, params):
            if sql.startswith("SELECT id FROM institutions"):
                return [{"id": rid} for rid in self.values["institutions"]]
            return []

        async def one(self, sql, params):
            if "tt_demo_restore_allowed" in sql:
                return {"allowed": True}
            table = re.search(r"FROM (\w+)", sql).group(1)
            return {"total": len(self.values[table])}

        async def execute(self, sql, params=None):
            await super().execute(sql, params)
            match = re.match(r"DELETE FROM (\w+)", str(sql))
            if match:
                self.values[match[1]].clear()

        async def insert(self, table, value):
            if table != "institutions":
                assert self.values["institutions"]
            self.values[table][value["id"]] = deepcopy(value)
            return value

    repo = ResetRepo()
    sample = deepcopy(samples[account])
    asyncio.run(
        restore.restore_to_checkpoint(repo, sample, 100, allow_initial_seed=True)
    )
    iid = sample.owned_tenant_ids[0]
    repo.values["institutions"][iid]["profile"]["name"] = "Changed during session"
    asyncio.run(
        restore.restore_to_checkpoint(repo, sample, 200, allow_initial_seed=True)
    )
    assert repo.values["institutions"][iid]["profile"]["name"].startswith(
        "Demo Institution"
    )
    assert all(
        value["fixture_generation"] == 200
        for value in repo.values["processing_jobs"].values()
    )
