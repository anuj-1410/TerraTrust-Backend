"""Recovery, model applicability and exported-byte tests without live services."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pypdf import PdfReader

from app.config import settings
from app.institution_errors import InstitutionError
from models.institution.common import canonical, digest
from services.advisory import provider_operations
from services.institution.analytics_service import emission_slice
from services.institution import report_service
from services.shared.model_applicability import validate as model_applicability


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "unknown", "expired"])
async def test_provider_checkpoint_survives_task_retry_and_unknown_never_resends(monkeypatch, outcome):
    job_id, owner = str(uuid4()), str(uuid4())
    state = {"row": None, "sends": 0}
    job = {"status": "RUNNING", "lease_owner": owner, "deadline_at": datetime.now(timezone.utc) + timedelta(minutes=5), "lease_expires_at": datetime.now(timezone.utc) + timedelta(minutes=-1 if outcome == "expired" else 1)}
    class Connection:
        async def execute(self, sql, params):
            statement = str(sql)
            if statement.startswith("INSERT INTO institution_provider_operations"):
                state["row"] = {"id": params["id"], "status": "STARTED"}
            elif "SET status='SUCCEEDED'" in statement:
                import json
                state["row"].update(status="SUCCEEDED", response=json.loads(params["response"]))
            elif "SET status=:state" in statement:
                state["row"]["status"] = params["state"]
    connection = Connection()
    class Repository:
        def __init__(self, conn):
            self.conn = conn
        async def get(self, *args):
            return job
        async def one(self, *args, **kwargs):
            return state["row"]
    class Begin:
        async def __aenter__(self):
            return connection
        async def __aexit__(self, *args):
            return False
    engine = SimpleNamespace(begin=lambda: Begin())
    monkeypatch.setattr(provider_operations, "Repository", Repository)
    async def send():
        state["sends"] += 1
        if outcome == "unknown":
            raise InstitutionError(503, "EXTERNAL_OUTCOME_UNKNOWN", "Unconfirmed")
        return {"candidates": [{"id": "real-response-shape-in-fixture"}]}
    token = provider_operations.current_operation.set((engine, job_id, owner))
    try:
        for _ in range(2):
            if outcome == "success":
                response = await provider_operations.invoke("generateContent", "commissioned-test-model", {"facts": []}, send)
                assert response["candidates"]
            else:
                with pytest.raises(InstitutionError) as error:
                    await provider_operations.invoke("generateContent", "commissioned-test-model", {"facts": []}, send)
                assert error.value.code == ("JOB_LEASE_LOST" if outcome == "expired" else "EXTERNAL_OUTCOME_UNKNOWN")
        assert state["sends"] == (0 if outcome == "expired" else 1)
    finally:
        provider_operations.current_operation.reset(token)


def model_context():
    facts = {"domain", "campus_id", "country", "state", "species_codes", "pools", "eligible_area_ha", "assessment_month", "observation_start", "observation_end"}
    domain = {"type": "object", "additionalProperties": False, "properties": {key: {} for key in facts}, "required": ["domain", "country"]}
    domain["properties"]["domain"] = {"const": "INSTITUTION"}
    domain["properties"]["country"] = {"const": "TEST_COUNTRY"}
    config = {"mode": "PRETRAINED_DOMAIN", "training_domain": domain, "validation_domain": deepcopy(domain), "applicability_schema": deepcopy(domain), "feature_schema": {"window_rule": "REQUEST_INCLUSIVE_DATES_V1", "mask_rule": "APPROVED_ELIGIBLE_MASK_V1", "fractional_pixel_rule": "POSTGIS_GEODESIC_PIXEL_INTERSECTION_V1"}}
    return {"model": {"config": config}, "request": {"assessment_date": "2024-12-31", "observation_start": "2024-12-01", "observation_end": "2024-12-31"}, "campus": {"id": str(uuid4()), "address": {"country": "TEST_COUNTRY", "state": "TEST_STATE"}}, "survey": {"measurements": [{"raw_input": {"species_code": "TEST_ONLY"}}]}, "mask": {"eligible_area_ha": "1"}, "method": {"config": {"pools": ["ABOVEGROUND_LIVE"]}}}


@pytest.mark.parametrize("change,code", [("country", "MODEL_OUTSIDE_VALIDATED_DOMAIN"), ("campus", "CAMPUS_CALIBRATION_REQUIRED"), ("rule", "UNSUPPORTED_FEATURE_RULE")])
def test_model_cannot_escape_its_actual_reviewed_domain(change, code):
    frozen = model_context()
    assert model_applicability(frozen)["species_codes"] == ["TEST_ONLY"]
    if change == "country":
        frozen["campus"]["address"]["country"] = "UNVALIDATED_COUNTRY"
    elif change == "campus":
        frozen["model"]["config"]["mode"] = "APPROVED_CAMPUS_CALIBRATION"
    else:
        frozen["model"]["config"]["feature_schema"]["window_rule"] = "UNIMPLEMENTED_WINDOW"
    with pytest.raises(InstitutionError) as error:
        model_applicability(frozen)
    assert error.value.code == code


def test_explicit_frozen_campus_scope_category_intersection_is_exact():
    a, b, x, y = [str(uuid4()) for _ in range(4)]
    frozen = {"audit": {"campus_ids": [a, b]}, "emissions": [{"id": x, "campus_id": a, "category": "ELECTRICITY"}, {"id": y, "campus_id": b, "category": "ELECTRICITY"}]}
    output = {"by_category": {"ELECTRICITY": "30"}, "by_scope": {"SCOPE_2": "30"}, "calculation_lines": [{"input_id": x, "included_contribution": True, "scope": "SCOPE_2", "contribution_amount": "10"}, {"input_id": y, "included_contribution": True, "scope": "SCOPE_2", "contribution_amount": "20"}]}
    assert emission_slice(output, frozen, {a}, "SCOPE_2", "ELECTRICITY") == 10
    frozen["emissions"][1]["campus_id"] = None
    assert emission_slice(output, frozen, {a}, "SCOPE_2", "ELECTRICITY") is None


def manifest():
    aid, sid = str(uuid4()), str(uuid4())
    return {"frozen_input": {"emissions": [{"id": str(uuid4()), "unit": "kWh", "source_id": "=HYPERLINK(unsafe)", "notes": "<b>source text</b>"}], "mitigations": [], "factors": {}}, "result": {"snapshot_id": sid, "audit_id": aid, "actual_total": "1", "inventory_balance": "-1", "calculation_lines": [], "input_hash": "a" * 64, "result_hash": "b" * 64}, "draft_watermark": True, "data_origin": "SYNTHETIC", "warnings": []}


def test_export_formats_preserve_frozen_values_and_escape_untrusted_markup(monkeypatch):
    monkeypatch.setattr(settings, "INSTITUTION_REPORT_FONT_PATH", "")
    frozen = manifest()
    original = digest(frozen)
    json_bytes, mime = report_service.render(frozen, "JSON")
    assert mime == "application/json" and json_bytes == canonical(frozen)
    csv_bytes, mime = report_service.render(frozen, "CSV")
    assert mime == "text/csv" and b"'=HYPERLINK(unsafe)" in csv_bytes
    assert b"'-1" in csv_bytes
    pdf, mime = report_service.render(frozen, "PDF")
    assert mime == "application/pdf" and pdf.startswith(b"%PDF-")
    extracted = "".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)
    assert "source text" in extracted and "DRAFT" in extracted and "DEMONSTRATION" in extracted
    assert original == digest(frozen)


def test_report_rejects_missing_unicode_glyphs_instead_of_silent_corruption(monkeypatch):
    monkeypatch.setattr(settings, "INSTITUTION_REPORT_FONT_PATH", "")
    frozen = manifest()
    frozen["frozen_input"]["emissions"][0]["notes"] = "भारतीय संस्था"
    with pytest.raises(InstitutionError) as error:
        report_service.render(frozen, "PDF")
    assert error.value.code == "REPORT_FONT_NOT_CONFIGURED"
    # JSON/CSV retain Unicode regardless of the PDF font deployment.
    assert "भारतीय" in report_service.render(frozen, "JSON")[0].decode()


def test_reviewed_unicode_font_is_checksummed_and_shaped(monkeypatch):
    import reportlab
    path = Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"
    monkeypatch.setattr(settings, "INSTITUTION_REPORT_FONT_PATH", str(path))
    monkeypatch.setattr(settings, "INSTITUTION_REPORT_FONT_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    frozen = manifest()
    frozen["frozen_input"]["emissions"][0]["notes"] = "Évalué"
    pdf, _ = report_service.render(frozen, "PDF")
    assert "Évalué" in "".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)
    monkeypatch.setattr(settings, "INSTITUTION_REPORT_FONT_SHA256", "0" * 64)
    with pytest.raises(InstitutionError):
        report_service.render(frozen, "PDF")


def test_demo_reset_is_rejected_in_production_even_if_flag_is_set(monkeypatch):
    from demo.institution.registry import require_demo
    monkeypatch.setattr(settings, "INSTITUTION_SCHEMA_ENABLED", True)
    monkeypatch.setattr(settings, "ENABLE_INSTITUTION_DEMO_ACCOUNTS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    with pytest.raises(InstitutionError) as error:
        require_demo()
    assert error.value.code == "DEMO_NOT_AVAILABLE"


def test_fresh_demo_manifest_cannot_capture_an_existing_tenant():
    from demo.institution.manifest import CheckpointManifest
    from pydantic import ValidationError
    value = {"schema_version": 1, "manifest_id": str(uuid4()), "manifest_version": 1, "checkpoint_key": "INSTITUTION_FRESH", "account_number": 5, "target_firebase_uid": "ACTUAL_TEST_UID_FIXTURE", "target_user_id": str(uuid4()), "deployment_id": "TEST_ONLY", "deployment_env": "test", "owned_tenant_ids": [], "created_at": "2026-10-21T00:00:00Z", "created_by": str(uuid4()), "prerequisites": {"method_ids": [], "factor_ids": [], "model_ids": [], "quality_policy_ids": [], "corpus_ids": [], "config_hash": "a" * 64}, "user_checkpoint": {"onboarding_status": "REQUIRED", "onboarding_completed_at": None, "registration_id": None, "registration_institution_id": None}, "tables": [], "objects": [], "expected": {"landing_screen": "Institution Setup", "permission": None, "tenant_count": 0, "campus_count": 0, "survey_count": 0, "tree_audit_count": 0, "audit_count": 0, "current_final_audit_id": None, "result_hash": None, "export_ids": [], "published_query_ids": []}, "provenance": {"prepared_via_normal_services": True, "review_case_ids": [], "source_record_ids": [], "validation_record_id": str(uuid4())}}
    value["manifest_sha256"] = digest(value)
    assert CheckpointManifest.model_validate(value).account_number == 5
    value["owned_tenant_ids"] = [str(uuid4())]
    value["manifest_sha256"] = digest({k: v for k, v in value.items() if k != "manifest_sha256"})
    with pytest.raises(ValidationError):
        CheckpointManifest.model_validate(value)


@pytest.mark.parametrize("case", ["valid", "empty", "missing", "limit"])
def test_satellite_composition_is_bound_to_the_recorded_image_ids(monkeypatch, case):
    from services.shared import satellite_service as satellite
    rows = [{"asset_id": "reviewed-test-dataset/b", "time_start_ms": 1700000000000}, {"asset_id": "reviewed-test-dataset/a", "time_start_ms": 1700000000001}]
    if case == "empty":
        rows = []
    elif case == "missing":
        rows[0]["time_start_ms"] = None
    elif case == "limit":
        monkeypatch.setattr(satellite, "MAX_SOURCE_IMAGES", 1)
    class Image:
        def __init__(self, value):
            self.value = value
        def id(self):
            return self.value["asset_id"]
        def get(self, key):
            return self.value["time_start_ms"]
    class Values:
        def map(self, function):
            return SimpleNamespace(getInfo=lambda: [function(row) for row in rows])
    ee = SimpleNamespace(Image=Image, Dictionary=lambda row: row, ImageCollection=SimpleNamespace(fromImages=lambda images: images))
    collection = SimpleNamespace(toList=lambda limit: Values())
    if case == "valid":
        pinned, manifest = satellite.pin_collection(ee, collection, "reviewed-test-dataset")
        assert [image.value for image in pinned] == [row["asset_id"] for row in manifest["images"]]
        assert pinned[0].value.endswith("/a")
    else:
        with pytest.raises(InstitutionError) as error:
            satellite.pin_collection(ee, collection, "reviewed-test-dataset")
        assert error.value.code == {"empty": "SATELLITE_NEEDS_DATA", "missing": "SATELLITE_PROVENANCE_MISSING", "limit": "SOURCE_IMAGE_LIMIT"}[case]


def test_reporting_boundary_change_blocks_an_apparently_comparable_trend():
    from services.institution.analytics_service import compare, comparison_context
    previous = {"actual_total": "10", "coverage": {}, "versions": {"method_version": "same", "factor_versions": []}, "period_start": "2023-01-01", "period_end": "2023-12-31", "_comparison_context": comparison_context({"audit": {"campus_ids": ["campus-a"]}}, "actual_total")}
    current = {**previous, "actual_total": "12", "period_start": "2024-01-01", "period_end": "2024-12-31", "_comparison_context": comparison_context({"audit": {"campus_ids": ["campus-b"]}}, "actual_total")}
    delta, percentage, reason = compare(previous, current, "actual_total")
    assert delta is None and percentage is None and reason == "REPORTING_BOUNDARY_CHANGED"
