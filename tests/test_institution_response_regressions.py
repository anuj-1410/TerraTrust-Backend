"""Exercise service responses against Python and the actual mobile decoder."""

import ast
import asyncio
import json
import shutil
import subprocess
import sys
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.datastructures import QueryParams

from app.config import settings
from demo.institution.sample_data import build_sample
from models.institution.common import digest, normalize
from models.institution.contracts import MODELS
from repositories.institution.base import TABLES, Repository, columns
from services.institution import (
    analytics_service,
    annual_service,
    campus_service,
    institution_service,
    registry_service,
    survey_service,
)
from services.institution.audit_projection import denominators, tree_link
from services.institution.base import public

ROOT = Path(__file__).resolve().parents[1]
USER = "d88a4ab6-1c33-423c-a589-7ac7ad36b94e"


class Repo:
    def __init__(self, sample):
        self.conn = self
        self.tables = {bundle.table: deepcopy(bundle.rows) for bundle in sample.tables}
        for table, row in sample.globals:
            self.tables.setdefault(table, []).append(deepcopy(row))
        # Convert date/numeric columns exactly as the PostgreSQL driver does.
        for table, rows in self.tables.items():
            for row in rows:
                for key, spec in TABLES[table].items():
                    if key not in row:
                        if spec["type"].endswith("[]"):
                            row[key] = []
                        elif spec["type"] == "JSONB":
                            row[key] = {}
                    value = row.get(key)
                    if value is not None and spec["type"] == "DATE":
                        row[key] = date.fromisoformat(value)
                    elif value is not None and spec["type"] == "TIMESTAMPTZ":
                        row[key] = datetime.fromisoformat(value)
                    elif value is not None and spec["type"].startswith("NUMERIC"):
                        row[key] = Decimal(value)
        self.writes = []

    async def execute(self, sql, params=None):
        self.writes.append((str(sql), params))

    async def get(self, table, rid, **kwargs):
        result = next(
            (row for row in self.tables.get(table, []) if str(row["id"]) == str(rid)),
            None,
        )
        if result is None and kwargs.get("required", True):
            raise AssertionError((table, rid))
        return deepcopy(result)

    async def find(self, table, filters, **kwargs):
        return [
            deepcopy(row)
            for row in self.tables.get(table, [])
            if all(row.get(key) == value for key, value in filters.items())
        ]

    async def insert(self, table, row):
        assert set(row) <= columns(table), (table, set(row) - columns(table))
        Repository(None).binds(table, row)
        self.writes.append((table, deepcopy(row)))
        self.tables.setdefault(table, []).append(deepcopy(row))
        return row


@pytest.fixture(scope="module")
def sample():
    return build_sample(9, "offline-contracts-nine", USER)


def context(repo, **overrides):
    iid = repo.tables["institutions"][0]["id"]
    return SimpleNamespace(
        repo=repo,
        iid=iid,
        actor={"id": USER},
        member={"permission": "OWNER"},
        request=SimpleNamespace(query_params=QueryParams()),
        context_id=None,
        **overrides,
    )


def test_legacy_denominators_are_projected_without_rewriting_history(sample):
    repo = Repo(sample)
    audit = repo.tables["institution_audits"][0]
    audit["denominators"] = {"student_count": 600}
    before = digest(audit)
    output = asyncio.run(annual_service.resource(context(repo), audit))
    MODELS["AuditResource"].model_validate(output)
    assert output["denominators"] == {
        "students": 600,
        "staff": None,
        "floor_area_m2": None,
        "source_evidence_ids": [],
        "as_of": None,
    }
    assert digest(audit) == before and repo.writes == []
    assert denominators({})["students"] is None


@pytest.mark.parametrize(
    "metric",
    [
        "actual_total",
        "tree_stock",
        "removal",
        "release",
        "inventory_balance",
        "avoided_total",
    ],
)
def test_analytics_supports_existing_sample_history_and_all_metrics(sample, metric):
    repo = Repo(sample)
    for audit in repo.tables["institution_audits"]:
        audit["denominators"] = {"student_count": 600}
    # Initial sample snapshots omitted these optional attribution snapshots.
    for row in repo.tables["institution_calculation_snapshots"]:
        for link in row["frozen_input"]["tree_links"]:
            link["attribution_snapshot"].pop("boundary", None)
            link["attribution_snapshot"].pop("mask", None)
    ctx = context(repo)
    ctx.request.query_params = QueryParams({"metric": metric})
    output = asyncio.run(analytics_service.analytics(ctx))
    MODELS["AnalyticsResource"].model_validate(output)
    assert len(output["periods"]) == 2 and repo.writes == []
    assert (
        Decimal(output["periods"][0]["intensities"]["students"])
        == Decimal("92.094") / 600
    )


def test_live_tree_link_save_freeze_and_content_use_actual_sql_columns(
    sample, monkeypatch
):
    repo = Repo(sample)
    audit = repo.tables["institution_audits"][1]
    audit["business_status"] = "DRAFT"
    stock = repo.tables["campus_tree_audits"][1]
    stock.update(
        comparison_start=audit["period_start"], comparison_end=audit["period_end"]
    )
    repo.tables["institution_audit_tree_links"] = []
    ctx = context(repo, parent=audit, match=lambda _: None)
    monkeypatch.setattr(annual_service, "require_flow", lambda: None)

    async def refresh(*args):
        ctx.revision = 2

    monkeypatch.setattr(annual_service, "refresh_hash", refresh)
    item = {
        "tree_audit_id": stock["id"],
        "campus_id": stock["campus_id"],
        "period_start": str(audit["period_start"]),
        "period_end": str(audit["period_end"]),
        "comparability_status": "COMPARABLE",
        "reason": "Accepted assessment",
    }
    asyncio.run(annual_service.tree_links(ctx, {"items": [item]}))
    saved = repo.tables["institution_audit_tree_links"][0]
    assert "attributed_removal_tCO2" not in saved
    assert tree_link(saved)["attributed_removal_tCO2"] == normalize(
        stock["removal_tCO2"]
    )
    assert (
        asyncio.run(annual_service.content(ctx, audit))["tree_links"][0][
            "attributed_removal_tCO2"
        ]
        is not None
    )
    # Actual DB-maintained factors are accepted; no sample-provider bypass.
    for factor in repo.tables["emission_factors"]:
        factor.update(
            source_version="DATABASE_CONFIGURATION:test",
            license="PROJECT_CONFIGURATION",
        )
    frozen = asyncio.run(annual_service.freeze(ctx, audit))
    assert frozen["tree_links"][0]["attributed_removal_tCO2"] == normalize(
        stock["removal_tCO2"]
    )


def test_real_and_demo_responses_with_actual_mobile_validator(sample, tmp_path):
    frontend = ROOT.parent / "TerraTrustAR"
    node = shutil.which("node")
    if not node or not (frontend / "node_modules/typescript").is_dir():
        pytest.skip(
            "Read-only mobile checkout with installed TypeScript is required for this cross-project check."
        )
    repo = Repo(sample)
    ctx = context(repo)
    analytics = asyncio.run(analytics_service.analytics(ctx))
    fixtures = [{"model": "AnalyticsResource", "value": analytics}]
    fixtures.extend(
        {"model": "AnalyticsPeriod", "value": row} for row in analytics["periods"]
    )
    for audit in repo.tables["institution_audits"]:
        audit["denominators"] = {
            "student_count": 600
        }  # Already-installed account nine.
        output = asyncio.run(annual_service.resource(ctx, audit))
        fixtures.extend(
            [
                {"model": "AuditResource", "value": output},
                {"model": "Denominators", "value": output["denominators"]},
            ]
        )
    for table, kind in [
        ("carbon_methodologies", "METHOD"),
        ("emission_factors", "FACTOR"),
    ]:
        for value in repo.tables[table]:
            for origin in ["LIVE", "SYNTHETIC"]:
                output = registry_service.resource(
                    kind, {**value, "data_origin": origin}
                )
                MODELS["RegistryResource"].model_validate(output)
                fixtures.append({"model": "RegistryResource", "value": output})

    async def append_resources():
        from routers.institution_reports import export_resource

        for row in repo.tables["institution_exports"]:
            row["advice_hashes"] = {}  # Initial already-installed sample export.
            fixtures.append(
                {"model": "ExportResource", "value": await export_resource(ctx, row)}
            )
        for row in repo.tables["institution_audits"]:
            fixtures.append(
                {
                    "model": "ResultResource",
                    "value": await annual_service.result(ctx, row),
                }
            )
        for row in repo.tables["institutions"]:
            fixtures.append(
                {
                    "model": "InstitutionResource",
                    "value": await institution_service.institution_resource(ctx, row),
                }
            )
        for row in repo.tables["campuses"]:
            fixtures.append(
                {
                    "model": "CampusResource",
                    "value": await campus_service.resource(ctx, row),
                }
            )
        for row in repo.tables["campus_tree_surveys"]:
            fixtures.append(
                {
                    "model": "SurveyResource",
                    "value": await survey_service.survey_resource(ctx, row),
                }
            )
        for row in repo.tables["campus_tree_measurements"]:
            fixtures.append(
                {
                    "model": "MeasurementResource",
                    "value": survey_service.measurement_resource(row),
                }
            )
        for row in repo.tables["campus_tree_audits"]:
            fixtures.append(
                {
                    "model": "TreeAuditResource",
                    "value": survey_service.tree_audit_resource(row),
                }
            )
        for table, model, extra in [
            ("campus_boundaries", "BoundaryResource", {"overlap_flags": []}),
            ("campus_vegetation_masks", "MaskResource", {"quality_flags": []}),
            ("campus_sampling_plots", "PlotResource", {}),
            ("campus_trees", "TreeResource", {}),
        ]:
            for row in repo.tables[table]:
                fixtures.append({"model": model, "value": public(model, row, **extra)})

    asyncio.run(append_resources())
    for fixture in fixtures:
        MODELS[fixture["model"]].model_validate(fixture["value"])
    payload = tmp_path / "wire-fixtures.json"
    payload.write_text(json.dumps(normalize(fixtures)), encoding="utf-8")
    checked = subprocess.run(
        [
            node,
            str(ROOT / "frontend-integration/verify-institution-contracts.cjs"),
            str(frontend),
            str(payload),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    result = json.loads(checked.stdout)
    assert result["checked"] == len(fixtures) and result["patched_validator"] == "PASS"
    original = (frontend / "src/features/institution/contracts/schemas.ts").read_text(
        encoding="utf-8"
    )
    if "type === 'DraftRegistryConfig'" not in original:
        assert any(
            "RegistryResource/config" in error for error in result["original_failures"]
        )
    if "type.includes(' or ')" in original:
        assert any(
            "AnalyticsPeriod/by_scope" in error for error in result["original_failures"]
        )


def test_old_sample_tree_metadata_is_projected_without_altering_saved_tree(sample):
    row = deepcopy(sample.tables[0].rows[0])
    row.update(
        metadata={"sample": True},
        species_code="Neem",
        stable_tag="DEMO-001",
        condition="LIVE",
    )
    before = deepcopy(row)
    result = public("TreeResource", row)
    assert result["metadata"] == {"notes": "Synthetic demonstration sample."}
    assert row == before


def test_denominator_evidence_is_included_in_the_frozen_manifest(sample, monkeypatch):
    repo = Repo(sample)
    audit = repo.tables["institution_audits"][0]
    evidence_id = "282bab65-f8fe-4676-af53-2fa7c5e9339c"
    audit["denominators"]["source_evidence_ids"] = [evidence_id]
    for factor in repo.tables["emission_factors"]:
        factor.update(
            source_version="DATABASE_CONFIGURATION:test",
            license="PROJECT_CONFIGURATION",
        )

    async def evidence(repo, ids, *args, **kwargs):
        return [{"id": rid, "sha256": "a" * 64, "size_bytes": 100} for rid in ids]

    monkeypatch.setattr(annual_service, "evidence", evidence)
    output = asyncio.run(annual_service.freeze(context(repo), audit))
    assert evidence_id in output["evidence_manifest"]


def test_new_released_sample_carries_complete_frozen_read_metadata(sample):
    from services.institution.visibility import released_campuses, released_resource

    repo = Repo(sample)
    ctx = context(repo)
    ctx.member = {"permission": "VIEWER", "status": "ACTIVE"}
    snapshot = repo.tables["institution_calculation_snapshots"][0]
    ctx.request.query_params = QueryParams({"snapshot_id": snapshot["id"]})
    assert len(released_campuses(snapshot["frozen_input"])) == 1
    for kind, table, model in [
        ("institution", "institutions", "InstitutionResource"),
        ("campus", "campuses", "CampusResource"),
        ("boundary", "campus_boundaries", "BoundaryResource"),
        ("mask", "campus_vegetation_masks", "MaskResource"),
        ("tree", "campus_trees", "TreeResource"),
        ("tree_audit", "campus_tree_audits", "TreeAuditResource"),
    ]:
        output = asyncio.run(released_resource(ctx, kind, repo.tables[table][0]["id"]))
        MODELS[model].model_validate(output)


def test_postgis_areas_match_frozen_metadata_and_actual_export_bytes(sample):
    from demo.institution.sample_data import prepare_sample

    source = deepcopy(sample)

    class AreaRepo:
        async def one(self, sql, params):
            assert "ST_Area" in sql
            return {"valid": True, "area": Decimal("10.123456789012")}

    asyncio.run(prepare_sample(AreaRepo(), source))
    tables = {bundle.table: bundle.rows for bundle in source.tables}
    for snapshot in tables["institution_calculation_snapshots"]:
        stored = snapshot["frozen_input"]["tree_links"][0]["attribution_snapshot"]
        assert (
            stored["boundary"]["area_ha"] == tables["campus_boundaries"][0]["area_ha"]
        )
        assert stored["mask"]["eligible_area_ha"] == "10.123456789012"
    for artifact, obj in zip(tables["institution_artifacts"], source.objects):
        assert artifact["byte_hash"] == obj.sha256 and artifact["size_bytes"] == len(
            obj.data
        )
        if obj.mime_type == "application/json":
            assert b"10.123456789012" in obj.data


def test_static_sql_writes_reference_physical_columns_only():
    checked = 0
    for directory in ("services/institution", "routers", "tasks"):
        for path in (ROOT / directory).glob("*.py"):
            for call in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (
                    not isinstance(call, ast.Call)
                    or not isinstance(call.func, ast.Attribute)
                    or call.func.attr not in {"insert", "update"}
                    or len(call.args) < 2
                ):
                    continue
                table = call.args[0]
                if (
                    not isinstance(table, ast.Constant)
                    or table.value not in TABLES
                    or table.value == "users"
                ):
                    continue
                position = 2 if call.func.attr == "update" else 1
                if len(call.args) <= position or not isinstance(
                    call.args[position], ast.Dict
                ):
                    continue
                keys = {
                    key.value
                    for key in call.args[position].keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                }
                assert keys <= columns(table.value), (
                    str(path),
                    call.lineno,
                    keys - columns(table.value),
                )
                checked += 1
    assert checked >= 100


@pytest.mark.parametrize(
    "parent,origin,allowed",
    [
        ("REPORT", "LIVE", True),
        ("EXPORT", "SYNTHETIC", True),
        ("EXPORT", "LIVE", False),
    ],
)
def test_report_download_authorizes_real_and_legacy_demo_artifacts(
    sample, parent, origin, allowed
):
    from app.institution_errors import InstitutionError
    from routers.institution_reports import member_artifact_access

    repo = Repo(sample)
    artifact = repo.tables["institution_artifacts"][0]
    artifact.update(parent_type=parent, data_origin=origin)
    ctx = context(repo)
    ctx.request.path_params = {"artifact_id": artifact["id"]}

    async def access(table, rid):
        value = await repo.get(table, rid)
        assert value["institution_id"] == ctx.iid
        return value

    ctx.access = access
    if allowed:
        asyncio.run(member_artifact_access(ctx))
        assert ctx.artifact["id"] == artifact["id"]
    else:
        with pytest.raises(InstitutionError) as error:
            asyncio.run(member_artifact_access(ctx))
        assert error.value.code == "RESOURCE_NOT_FOUND"
    assert repo.writes == []


@pytest.mark.parametrize("is_demo", [False, True])
@pytest.mark.parametrize("flow_enabled", [False, True])
def test_fresh_bootstrap_routes_to_setup_only_when_flow_enabled(
    monkeypatch, is_demo, flow_enabled
):
    from demo.institution import sessions
    from services.institution import auth_service

    monkeypatch.setattr(settings, "INSTITUTION_SCHEMA_ENABLED", True)
    monkeypatch.setattr(settings, "ENABLE_INSTITUTION_FLOW", flow_enabled)
    monkeypatch.setattr(settings, "ENABLE_INSTITUTION_DEMO_ACCOUNTS", is_demo)
    for name in (
        "INSTITUTION_PRIVACY_POLICY_URL",
        "INSTITUTION_SUPPORT_URL",
        "INSTITUTION_ACCOUNT_DELETION_URL",
    ):
        monkeypatch.setattr(settings, name, "")
    user = {
        "id": USER,
        "firebase_uid": "verified-user",
        "phone_number": "+919000000005" if is_demo else "+919876543210",
        "full_name": None,
        "kyc_completed": False,
        "wallet_address": None,
        "role": "INSTITUTION_ADMIN",
        "account_domain": "INSTITUTION",
        "institution_onboarding_status": "REQUIRED",
        "institution_onboarding_completed_at": None,
    }

    class Engine:
        def begin(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, *args):
            pass

        async def one(self, sql, *args, **kwargs):
            return user if "WHERE firebase_uid=" in sql else None

        async def rows(self, *args):
            return []

    async def identity(*args):
        return {"uid": user["firebase_uid"], "phone_number": user["phone_number"]}

    async def session(*args):
        return {
            "status": "READY",
            "account_number": 5,
            "checkpoint_key": "INSTITUTION_FRESH",
            "fixture_generation": 100,
            "reset_policy": "NEXT_LOGIN",
            "audit_demo_mode": True,
        }

    monkeypatch.setitem(
        sys.modules,
        "app.institution_database",
        SimpleNamespace(_require_async_engine=Engine),
    )
    monkeypatch.setattr(auth_service, "Repository", lambda conn: conn)
    monkeypatch.setattr(auth_service, "identity", identity)
    monkeypatch.setattr(sessions, "ensure_login", session)
    result = asyncio.run(auth_service.bootstrap("Bearer verified-token", "INSTITUTION"))
    assert result["institution_onboarding_status"] == "REQUIRED"
    assert result["capabilities"]["institution_registration"] is flow_enabled
    assert result["institution_memberships"] == []
