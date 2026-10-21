"""Deployment and workflow regressions with no database/provider connections."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import permutations
from pathlib import Path
from types import SimpleNamespace
import asyncio
import ssl
import subprocess
import sys

from dotenv import dotenv_values
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
import pytest

from app.config import settings
from app.institution_access import operator_access
from app.institution_context import Context
from app.institution_errors import InstitutionError, error_handler
from models.institution.contracts import parse
from services.institution.accounting_service import calculate_inventory
from services.institution.factor_service import validate_config
from services.shared.model_applicability import candidate
from test_institution_accounting import inventory, uid
from test_institution_lifecycle import MemoryRepo
from test_institution_recovery import model_context, manifest


def run(awaitable):
    return asyncio.run(awaitable)


def test_model_setup_keeps_valid_models_until_dates_are_selected():
    frozen = model_context()
    config = frozen["model"]["config"]
    known = {"domain": "INSTITUTION", "country": "TEST_COUNTRY", "campus_id": frozen["campus"]["id"], "pools": ["ABOVEGROUND_LIVE"]}
    for schema in (config[key] for key in ("training_domain", "validation_domain", "applicability_schema")):
        schema["required"].append("assessment_month")
        schema["$defs"] = {"country": {"const": "TEST_COUNTRY"}}
        schema["properties"]["country"] = {"$ref": "#/$defs/country"}
    assert candidate(config, known)
    assert not candidate(config, {**known, "country": "UNVALIDATED_COUNTRY"})


@pytest.mark.parametrize("order", list(permutations(range(3))))
def test_nested_allocations_do_not_depend_on_saved_row_order(order):
    frozen = inventory()
    parent = frozen["emissions"][0]
    child = {**deepcopy(parent), "id": uid(), "source_id": "child", "quantity": "200", "included_in_input_id": parent["id"], "allocation_ratio": "0.2"}
    grandchild = {**deepcopy(child), "id": uid(), "source_id": "grandchild", "quantity": "100", "included_in_input_id": child["id"], "allocation_ratio": "0.5"}
    records = [parent, child, grandchild]
    frozen["emissions"] = [records[index] for index in order]
    result, errors = calculate_inventory(frozen)
    assert not errors
    lines = {line["input_id"]: line for line in result["calculation_lines"]}
    assert result["actual_total"] == "1.200000000000"
    assert Decimal(lines[grandchild["id"]]["contribution_amount"]) == Decimal("0.12")


def test_same_meter_label_at_two_campuses_is_not_a_duplicate_source():
    frozen = inventory()
    second_campus = uid()
    frozen["audit"]["campus_ids"].append(second_campus)
    frozen["emissions"].append({**deepcopy(frozen["emissions"][0]), "id": uid(), "campus_id": second_campus})
    result, errors = calculate_inventory(frozen)
    assert not errors and Decimal(result["actual_total"]) == Decimal("2.4")


def test_negative_factors_cannot_be_published_or_reduce_actual_emissions():
    frozen = inventory()
    factor = next(iter(frozen["factors"].values()))
    factor["config"]["value"] = "-1"
    with pytest.raises(InstitutionError):
        validate_config("FACTOR", factor["config"])
    result, errors = calculate_inventory(frozen)
    assert errors and result["actual_total"] is None


def test_pdf_bytes_are_reproducible_for_the_same_frozen_manifest(monkeypatch):
    from services.institution.report_service import render
    monkeypatch.setattr(settings, "INSTITUTION_REPORT_FONT_PATH", "")
    frozen = manifest()
    assert render(frozen, "PDF")[0] == render(frozen, "PDF")[0]


def test_small_utf8_chunk_bound_does_not_silently_discard_characters():
    from services.advisory.ingestion_service import chunks
    with pytest.raises(InstitutionError) as error:
        list(chunks([(1, "A🌳B")], 3, 0))
    assert error.value.code == "CHUNK_BOUND_TOO_SMALL"
    assert "".join(text for _, text in chunks([(1, "A🌳B")], 4, 0)) == "A🌳B"


def test_env_example_documents_required_institution_configuration():
    values = dotenv_values(Path(__file__).resolve().parents[1] / ".env.example", interpolate=False)
    required = {"INSTITUTION_SCHEMA_ENABLED", "ENABLE_INSTITUTION_FLOW", "ENABLE_INSTITUTION_ADVISOR", "INSTITUTION_PRIVACY_POLICY_URL", "INSTITUTION_SUPPORT_URL", "INSTITUTION_ACCOUNT_DELETION_URL", "INSTITUTION_REPORT_FONT_PATH", "INSTITUTION_REPORT_FONT_SHA256", "GEMINI_API_KEY", "LLM_MODEL", "EMBEDDING_MODEL", "EMBEDDING_DIMENSION", "ENABLE_INSTITUTION_DEMO_ACCOUNTS"}
    assert required <= values.keys()
    assert all(hasattr(settings, key) for key in required)
    assert all(values[key] == "false" for key in ("INSTITUTION_SCHEMA_ENABLED", "ENABLE_INSTITUTION_FLOW", "ENABLE_INSTITUTION_ADVISOR", "ENABLE_INSTITUTION_DEMO_ACCOUNTS"))
    assert values["GEMINI_API_KEY"] == "" and settings.LLM_PROVIDER == "GEMINI"


def test_database_tls_verifies_the_server_for_both_domains(monkeypatch):
    from tasks import institution_tasks
    from app import institution_database
    monkeypatch.setattr(settings, "INSTITUTION_SCHEMA_ENABLED", True)
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+asyncpg://review:review@db.invalid/review")
    monkeypatch.setattr(settings, "DATABASE_SSL_CA_FILE", "")
    observed = []
    def create(*args, **kwargs):
        observed.append(kwargs["connect_args"]["ssl"])
        return object()
    monkeypatch.setattr(institution_tasks, "create_async_engine", create)
    monkeypatch.setattr(institution_database, "create_async_engine", create)
    monkeypatch.setattr(institution_database, "_engine", None)
    institution_tasks.engine()
    institution_database._require_async_engine()
    assert all(context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED for context in observed)
    # Import the actual shared farmer client in a fresh process; never connect.
    code = """
import os, socket
socket.socket.connect = lambda *args: (_ for _ in ()).throw(AssertionError('No services'))
os.environ.update(DATABASE_URL='postgresql+asyncpg://review:review@db.invalid/review', DATABASE_SSL_CA_FILE='')
from app.database import ssl_context
import ssl
assert ssl_context.check_hostname and ssl_context.verify_mode == ssl.CERT_REQUIRED
"""
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)


@pytest.mark.parametrize("global_grant", [True, False])
def test_source_commissioning_is_authorized_while_tenant_writes_stay_disabled(monkeypatch, global_grant):
    from app import institution_context as module
    monkeypatch.setattr(settings, "INSTITUTION_SCHEMA_ENABLED", True)
    monkeypatch.setattr(settings, "ENABLE_INSTITUTION_FLOW", False)
    monkeypatch.setattr(settings, "ENABLE_INSTITUTION_DEMO_ACCOUNTS", False)
    monkeypatch.setattr(module, "router", APIRouter())
    actor = {"id": uid(), "account_domain": "INSTITUTION"}
    repo = MemoryRepo({"operator_grants": [{"institution_id": None, "case_id": None}] if global_grant else []})
    class Begin:
        async def __aenter__(self):
            return repo.conn
        async def __aexit__(self, *args):
            return False
    async def user(*args):
        if len(args) == 3:
            assert args[-1] == "UPDATE"
        return actor
    async def identity(*args):
        return {"uid": "offline-operator"}
    async def no_limit(*args):
        return None
    monkeypatch.setitem(sys.modules, "app.institution_database", SimpleNamespace(_require_async_engine=lambda: SimpleNamespace(begin=lambda: Begin())))
    monkeypatch.setattr(module, "Repository", lambda conn: repo)
    monkeypatch.setattr(module, "identity", identity)
    monkeypatch.setattr(module, "user_for_claims", user)
    monkeypatch.setattr(Context, "rate_limit", no_limit)
    async def authorized(ctx):
        await operator_access(ctx.repo, ctx.actor, "METHOD_REVIEW")
    @module.endpoint("POST", "/admin/carbon-methodologies", status=201, prepare=authorized)
    async def source(ctx, body):
        return {"id": uid()}
    @module.endpoint("POST", "/institutions")
    async def tenant(ctx, body):
        pytest.fail("Business writes must remain disabled")
    app = FastAPI()
    app.add_exception_handler(InstitutionError, error_handler)
    app.include_router(module.router, prefix="/api/v1")
    with TestClient(app) as client:
        headers = {"Idempotency-Key": "commissioning-test"}
        result = client.post("/api/v1/admin/carbon-methodologies", json={}, headers=headers)
        assert result.status_code == (201 if global_grant else 404)
        assert client.post("/api/v1/institutions", json={}, headers=headers).status_code == 503


def test_operator_source_upload_has_no_active_method_bootstrap_dependency(monkeypatch):
    from services.institution import evidence_service as service
    monkeypatch.setattr(settings, "INSTITUTION_SCHEMA_ENABLED", True)
    monkeypatch.setattr(settings, "ENABLE_INSTITUTION_FLOW", False)
    actor, parent_id = {"id": uid()}, uid()
    class SourceRepo(MemoryRepo):
        async def insert(self, table, values):
            if table == "institution_audit_evidence":
                values = {"byte_hash": None, "actual_size_bytes": None, **values}
            return await super().insert(table, values)
    repo = SourceRepo({"operator_grants": [{"institution_id": None, "case_id": None}], "carbon_methodologies": [{"id": parent_id, "author_id": actor["id"], "status": "DRAFT"}]})
    request = Request({"type": "http", "scheme": "https", "server": ("testserver", 443), "path": "/api/v1/evidence/uploads", "headers": [], "query_string": b""})
    ctx = SimpleNamespace(repo=repo, actor=actor, iid=None, request=request, generation=None)
    body = {"parent_type": "METHOD_SOURCE", "parent_id": parent_id, "file_name": "source.pdf", "mime_type": "application/pdf", "size_bytes": 4, "expected_sha256": "a" * 64}
    upload = run(service.create_upload(ctx, body))
    evidence = repo.tables["institution_audit_evidence"][0]
    evidence["state"] = "UPLOADED"
    ctx.evidence = evidence
    accepted = run(service.complete(ctx, {"parent_id": parent_id, "expected_sha256": "a" * 64}))
    assert upload["upload_url"].startswith("https://testserver/api/v1/evidence/uploads/")
    assert accepted["status"] == "QUEUED" and evidence["state"] == "CHECKING"
    with pytest.raises(InstitutionError):
        service.require_upload_configuration("AUDIT")


def test_import_commit_rejects_a_factor_deactivated_since_preview(monkeypatch):
    from services.institution import import_service
    frozen = inventory()
    audit = {**frozen["audit"], "business_status": "DRAFT"}
    iid, eid, bid, actor_id = uid(), uid(), uid(), uid()
    frozen["emissions"][0]["evidence_ids"] = [eid]
    factor = next(iter(frozen["factors"].values()))
    factor["status"] = "DEACTIVATED"
    method = {"id": audit["method_version"], "status": "APPROVED", "data_origin": "SYNTHETIC", "config": {"factor_mappings": [{"category": "ELECTRICITY", "subcategory": "GRID", "process": "GRID", "allowed_factor_ids": [factor["id"]]}]}}
    batch = {"id": bid, "audit_id": audit["id"], "evidence_id": eid, "valid_count": 1, "error_count": 0, "duplicate_count": 0, "status": "PREVIEWED", "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5), "source_input_revision": 1, "file_hash": "b" * 64, "normalized_hash": "c" * 64, "committed_input_revision": None}
    evidence = {"id": eid, "institution_id": iid, "parent_type": "AUDIT", "parent_id": audit["id"], "state": "READY", "byte_hash": "b" * 64, "bytes_deleted_at": None, "actual_size_bytes": 1}
    repo = MemoryRepo({"institution_import_batches": [batch], "institution_import_rows": [{"id": uid(), "batch_id": bid, "row_number": 2, "line_id": frozen["emissions"][0]["id"], "normalized_line": frozen["emissions"][0], "errors": [], "warnings": []}], "institution_audit_evidence": [evidence], "carbon_methodologies": [method], "emission_factors": [factor]})
    async def page(rows, *args):
        return {"items": rows}
    ctx = SimpleNamespace(repo=repo, parent=audit, iid=iid, actor={"id": actor_id}, match=lambda row: None, request=SimpleNamespace(query_params={}), page_rows=page)
    monkeypatch.setattr(import_service, "require_flow", lambda: None)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    with pytest.raises(InstitutionError) as error:
        run(import_service.commit(ctx, {"preview_id": bid, "normalized_hash": "c" * 64, "expected_input_revision": 1}))
    assert error.value.code == "INCOMPATIBLE_FACTOR"
    assert not repo.tables.get("institution_emission_inputs")


@pytest.mark.parametrize("name,body", [("ImportCommitRequest", {"preview_id": "bad", "normalized_hash": "a" * 64, "expected_input_revision": True}), ("ReviewClaimRequest", {"current_revision": True, "action": "CLAIM"}), ("RegistrationAuthorizationRequest", {"target_user_id": "bad", "reason": "Review"})])
def test_administrative_identifiers_and_counters_fail_before_database_binds(name, body):
    with pytest.raises(ValueError):
        parse(name, body)


def test_source_dns_outage_keeps_a_safe_retry_path(monkeypatch):
    from services.advisory import public_source_service
    def unavailable(*args, **kwargs):
        raise OSError("offline DNS simulation")
    monkeypatch.setattr(public_source_service.socket, "getaddrinfo", unavailable)
    with pytest.raises(InstitutionError) as error:
        public_source_service.fetch("https://source.invalid/document.pdf", {"source.invalid"})
    assert error.value.code == "PUBLIC_SOURCE_UNAVAILABLE" and error.value.retryable


def test_deleted_requesting_identity_cannot_finish_a_background_job():
    from tasks.institution_tasks import guarded
    actor_id = uid()
    repo = MemoryRepo({"users": [{"id": actor_id, "firebase_uid": None, "phone_number": None}]})
    with pytest.raises(InstitutionError) as error:
        run(guarded(repo, {"requested_by": actor_id, "deadline_at": datetime.now(timezone.utc) + timedelta(minutes=5)}))
    assert error.value.code == "MEMBERSHIP_REVOKED"


def test_repeated_control_hash_can_create_a_new_review_after_intervening_edits():
    from services.institution.base import case_for
    iid, cid, actor = uid(), uid(), {"id": uid()}
    repo = MemoryRepo()
    original = {"id": cid, "institution_id": iid, "created_by": actor["id"], "control_input_hash": "a" * 64, "input_revision": 2}
    first = run(case_for(repo, actor, "campuses", original, "CAMPUS_CONTROL"))
    second = run(case_for(repo, actor, "campuses", {**original, "input_revision": 4}, "CAMPUS_CONTROL"))
    assert first["revision"] == 2 and second["revision"] == 4
    assert repo.tables["review_cases"][0]["status"] == "CLOSED"


@pytest.mark.parametrize("changed,expected", [(None, True), ("season", False), ("plots", False), ("policy", False)])
def test_stock_comparison_requires_same_season_plots_and_quality_policy(changed, expected):
    from services.shared.carbon_stock_service import same_season
    pid = uid()
    frozen = {"request": {"observation_start": "2025-12-01", "observation_end": "2025-12-31"}, "survey": {"survey": {"plot_set_id": pid}, "policy": {"artifact_hash": "a" * 64, "config": {"temporal": {"revisit_matching_rule": "STABLE_TREE_ID_AND_PLOT_SET_V1", "seasonal_tolerance_days": 7}}}}}
    baseline = {"provenance": {"plot_set_id": pid, "quality_policy_hash": "a" * 64, "observation_window": {"observation_start": "2024-12-01", "observation_end": "2024-12-31"}}}
    if changed == "season":
        frozen["request"].update(observation_start="2025-07-01", observation_end="2025-07-31")
    elif changed == "plots":
        frozen["survey"]["survey"]["plot_set_id"] = uid()
    elif changed == "policy":
        frozen["survey"]["policy"]["artifact_hash"] = "b" * 64
    assert same_season(baseline, frozen) is expected


@pytest.mark.parametrize("assessment,code", [("2024-12-31", "OBSERVATION_WINDOW_TOO_LARGE"), ("2024-12-20", "INVALID_OBSERVATION_WINDOW")])
def test_model_window_must_match_the_actual_assessment_and_reviewed_policy(assessment, code):
    from services.shared.model_applicability import validate
    frozen = model_context()
    frozen["request"]["assessment_date"] = assessment
    frozen["survey"]["policy"] = {"config": {"temporal": {"max_window_days": 10}}}
    with pytest.raises(InstitutionError) as error:
        validate(frozen)
    assert error.value.code == code


@pytest.mark.parametrize("source_unit", ["MJ/kg", "MJ/L"])
def test_biogas_heat_uses_matching_heating_value_and_displaced_fuel_units(source_unit):
    from services.institution.mitigation_service import calculate_action, Unquantified
    frozen = inventory()
    factor = deepcopy(next(iter(frozen["factors"].values())))
    factor["config"].update(purpose="AVOIDED", denominator_unit="kg", value="3", category="STATIONARY_FUEL")
    parameters = {"gas_lhv": {"kind": "LHV", "value": "20", "unit": "MJ/std_m3"}, "gas_eff": {"kind": "EFFICIENCY", "value": "0.5", "unit": "fraction"}, "fuel_lhv": {"kind": "LHV", "value": "10", "unit": source_unit}, "fuel_eff": {"kind": "EFFICIENCY", "value": "0.5", "unit": "fraction"}}
    component = uid()
    line = {"action": "BIOGAS", "project": {"used_std_m3": "2", "produced_std_m3": "2", "methane_fraction": None, "LHV_source_id": "gas_lhv", "efficiency_source_id": "gas_eff", "route": "HEAT", "useful_output": None, "project_component_input_ids": [component]}, "baseline": {"heating_value_source_id": "fuel_lhv", "efficiency_source_id": "fuel_eff", "energy_factor_id": factor["id"], "treatment_components": []}}
    actual = {component: {"input_id": component, "contribution_amount": "0.001", "linked_parent_id": None}}
    if source_unit == "MJ/L":
        with pytest.raises(Unquantified):
            calculate_action(line, {factor["id"]: factor}, parameters, actual, [])
    else:
        avoided, baseline, project, _, _ = calculate_action(line, {factor["id"]: factor}, parameters, actual, [])
        assert baseline == Decimal("0.012") and project == Decimal("0.001") and avoided == Decimal("0.011")


def test_exported_published_advice_excludes_private_question_and_constraints(monkeypatch):
    from routers import institution_reports as reports
    iid, aid, sid, qid, actor_id = [uid() for _ in range(5)]
    result = {"result_hash": "a" * 64}
    audit = {"id": aid, "business_status": "FINALIZED", "reviewed_snapshot_id": sid, "data_origin": "LIVE", "reviewed_by": actor_id, "finalized_by": actor_id, "reviewed_at": None, "finalized_at": None}
    query = {"id": qid, "audit_id": aid, "snapshot_id": sid, "job_id": uid(), "user_id": actor_id, "question": "Private person and question", "constraints": {"notes": "Private site notes"}, "status": "READY", "publication_state": "PUBLISHED", "answer": {"answer": {"recommendations": [], "missing_data": []}, "sources": []}, "context_hash": "a" * 64, "answer_hash": "b" * 64, "context_snapshot": {}, "versions": {}, "warnings": [], "input_revision": 1}
    repo = MemoryRepo({"advisory_queries": [query], "institution_calculation_snapshots": [{"id": sid, "frozen_input": {"evidence_manifest": {}}}]})
    observed = {}
    async def load_result(*args):
        return result
    async def enqueue(ctx, kind, row, frozen, **kwargs):
        observed.update(frozen)
        return {"id": uid(), "resource_id": row["id"], "source_input_revision": 1, "status": "QUEUED", "job_type": kind}
    monkeypatch.setattr(reports, "require_flow", lambda: None)
    monkeypatch.setattr(reports.annual_service, "result", load_result)
    monkeypatch.setattr(reports, "enqueue", enqueue)
    ctx = SimpleNamespace(repo=repo, iid=iid, parent=audit, actor={"id": actor_id}, generation=None)
    run(reports.create_export(ctx, {"snapshot_id": sid, "expected_result_hash": "a" * 64, "format": "JSON", "include_advice": True}))
    saved = observed["manifest"]["advice"][0]
    assert saved["query_id"] == qid and saved["answer_hash"] == "b" * 64
    assert not {"question", "constraints", "user_id"} & saved.keys()
    assert repo.tables["institution_exports"][0]["advice_query_ids"] == [qid]


def test_preflight_is_offline_and_never_discloses_configuration_values(monkeypatch, tmp_path):
    import base64
    from services.shared import receipt_crypto_service
    key = tmp_path / "receipt.key"
    key.write_bytes(base64.urlsafe_b64encode(bytes(range(32))))
    monkeypatch.setattr(receipt_crypto_service, "KEY_FILE", key)
    import hashlib
    import json
    import reportlab
    from scripts.institution.preflight import check
    config = settings.model_copy(deep=True)
    fields = {"ACTIVE_ACCOUNTING_METHOD_VERSION": uid(), "BIOMASS_MODEL_VERSION": uid(), "ACTIVE_CORPUS_VERSION": uid(), "INSTITUTION_RECEIPT_KMS_KEY_ID": "projects/test/locations/test/keyRings/test/cryptoKeys/test", "INSTITUTION_PRIVACY_POLICY_URL": "https://site.invalid/privacy", "INSTITUTION_SUPPORT_URL": "https://site.invalid/support", "INSTITUTION_ACCOUNT_DELETION_URL": "https://site.invalid/delete", "GEMINI_API_KEY": "DO_NOT_DISCLOSE_THIS_KEY", "LLM_PROVIDER": "GEMINI", "EMBEDDING_PROVIDER": "GEMINI", "LLM_MODEL": "commissioned-test-model", "EMBEDDING_MODEL": "commissioned-test-embedding", "EMBEDDING_DIMENSION": 3, "DATABASE_SSL_CA_FILE": "", "LLM_API_KEY": "", "LLM_BASE_URL": "", "EMBEDDING_API_KEY": "", "EMBEDDING_BASE_URL": ""}
    for name, value in fields.items():
        if name in type(config).model_fields:
            setattr(config, name, value)
    path = Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"
    config.INSTITUTION_REPORT_FONT_PATH = str(path)
    config.INSTITUTION_REPORT_FONT_SHA256 = hashlib.sha256(path.read_bytes()).hexdigest()
    assert check(config, advisor=True) == []
    config.LLM_MODEL = "invalid/model"
    errors = check(config, advisor=True)
    assert errors == [{"setting": "LLM_MODEL", "code": "EXPLICIT_GEMINI_MODEL_ID_REQUIRED"}]
    assert "DO_NOT_DISCLOSE_THIS_KEY" not in json.dumps(errors)


def test_preflight_rejects_invalid_urls_and_ca_bundles(tmp_path):
    from scripts.institution.preflight import check
    config = settings.model_copy(deep=True)
    config.INSTITUTION_PRIVACY_POLICY_URL = "https://[invalid"
    pem = tmp_path / "invalid.pem"
    pem.write_text("not a certificate")
    config.DATABASE_SSL_CA_FILE = str(pem)
    errors = {row["setting"]: row["code"] for row in check(config)}
    assert errors["DATABASE_SSL_CA_FILE"] == "TRUSTED_CA_PEM_REQUIRED"
    assert errors["INSTITUTION_PRIVACY_POLICY_URL"] == "PUBLIC_HTTPS_URL_REQUIRED"


def test_released_tree_search_and_order_use_only_selected_snapshot(monkeypatch):
    from routers.campus_trees import trees
    from services.institution import visibility
    cid = uid()
    rows = [{"id": uid(), "stable_tag": tag, "species_code": "CODE"} for tag in ["B-2", "A-1", "C-3"]]
    async def snapshot(ctx):
        return {"frozen_input": {"tree_links": [{"campus_id": cid, "attribution_snapshot": {"survey": {"trees": rows}}}]}}
    async def page(selected, serializer):
        return selected
    monkeypatch.setattr(visibility, "released_snapshot", snapshot)
    ctx = SimpleNamespace(member={"permission": "VIEWER"}, parent={"id": cid}, request=SimpleNamespace(query_params={"search": "-", "sort": "-stable_tag"}), page_rows=page)
    assert [row["stable_tag"] for row in run(trees(ctx, {}))] == ["C-3", "B-2", "A-1"]
    ctx.request.query_params["search"] = "a-"
    assert [row["stable_tag"] for row in run(trees(ctx, {}))] == ["A-1"]
    ctx.request.query_params["sort"] = "invalid"
    with pytest.raises(InstitutionError) as error:
        run(trees(ctx, {}))
    assert error.value.code == "INVALID_SORT"
