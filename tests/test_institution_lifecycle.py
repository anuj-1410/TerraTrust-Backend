"""Lifecycle regressions with isolated repositories and no service connections."""
import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.config import settings
from app.institution_access import independent_reviewer, operator_access, tenant_access
from app.institution_context import Context
from app.institution_errors import InstitutionError, error_handler
from models.institution.common import digest
from services.institution import analytics_service, privacy_service, workflow_service
from services.advisory.advisory_service import provider_question


def uid():
    return str(uuid4())


class Connection:
    async def execute(self, *args, **kwargs):
        return SimpleNamespace(rowcount=1)


class MemoryRepo:
    def __init__(self, tables=None):
        self.tables = tables or {}
        self.conn = Connection()
        self.writes = []

    async def get(self, table, rid, *, required=True, lock=False):
        row = next((r for r in self.tables.get(table, []) if str(r["id"]) == str(rid)), None)
        if row is None and required:
            raise InstitutionError(404, "RESOURCE_NOT_FOUND", "Unavailable")
        return deepcopy(row)

    async def find(self, table, filters, **kwargs):
        return [deepcopy(r) for r in self.tables.get(table, []) if all(r.get(k) == v for k, v in filters.items())]

    async def rows(self, sql, params=None):
        if "operator_grants" in sql:
            return self.tables.get("operator_grants", [])
        if "FROM users" in sql:
            return self.tables.get("users", [])
        return []

    async def one(self, sql, params=None, *, required=True):
        rows = await self.rows(sql, params)
        return rows[0] if rows else None

    async def update(self, table, rid, values, *, bump=False):
        row = next(r for r in self.tables[table] if str(r["id"]) == str(rid))
        row.update(values)
        if bump:
            row["input_revision"] += 1
        self.writes.append((table, rid, deepcopy(values)))
        return deepcopy(row)

    async def insert(self, table, values):
        row = {"id": uid(), "input_revision": 1, **values}
        self.tables.setdefault(table, []).append(row)
        self.writes.append((table, row["id"], deepcopy(values)))
        return deepcopy(row)

    async def event(self, *args, **kwargs):
        return None


def run(awaitable):
    return asyncio.run(awaitable)


def test_operator_grant_cannot_expand_from_one_case_to_another():
    actor, case_id = {"id": uid()}, uid()
    repo = MemoryRepo({"operator_grants": [{"institution_id": None, "case_id": case_id}]})
    with pytest.raises(InstitutionError) as error:
        run(operator_access(repo, actor, "ACCOUNTING_REVIEW", iid=uid(), case={"id": uid()}))
    assert error.value.status == 404


@pytest.mark.parametrize("historical,current", [(True, False), (False, True)])
def test_reviewer_conflict_survives_revoked_membership(historical, current):
    iid, actor_id, case_id = uid(), uid(), uid()
    actor = {"id": actor_id}
    repo = MemoryRepo({"operator_grants": [{"institution_id": iid, "case_id": None}], "institution_users": [{"id": uid(), "institution_id": iid, "user_id": actor_id, "status": "REVOKED"}] if current else []})
    case = {"id": case_id, "institution_id": iid, "resource_type": "INSTITUTION_AUDIT", "resource_snapshot": {"contributor_user_ids": [], "historical_member_user_ids": [actor_id] if historical else []}}
    with pytest.raises(InstitutionError) as error:
        run(independent_reviewer(repo, actor, case))
    assert error.value.code == "REVIEWER_CONFLICT"


def test_pending_registration_access_does_not_extend_to_another_creator():
    iid, owner, stranger = uid(), uid(), uid()
    repo = MemoryRepo({"institutions": [{"id": iid, "created_by": owner, "verification_status": "PENDING"}]})
    run(tenant_access(repo, {"id": owner}, iid, "O", own_pending=True))
    with pytest.raises(InstitutionError):
        run(tenant_access(repo, {"id": stranger}, iid, "O", own_pending=True))


@pytest.mark.parametrize("expired,counter,expected", [(True, 3, "REVIEW_CLAIM_REQUIRED"), (False, 2, "CASE_REVISION_CONFLICT")])
def test_review_claim_requires_current_lease_and_case_counter(expired, counter, expected):
    actor_id = uid()
    ctx = SimpleNamespace(actor={"id": actor_id}, case={"status": "CLAIMED", "reason": "Review", "input_revision": 3, "assigned_reviewer": actor_id, "claim_expires_at": datetime.now(timezone.utc) + timedelta(minutes=-1 if expired else 1)})
    with pytest.raises(InstitutionError) as error:
        workflow_service.require_claim(ctx, counter)
    assert error.value.code == expected


def test_finalize_binds_exact_hash_and_preserves_old_final_until_replacement(monkeypatch):
    monkeypatch.setattr(workflow_service, "require_flow", lambda: None)
    iid, aid, old_id, family_id, sid, reviewer, case_id = [uid() for _ in range(7)]
    audit = {"id": aid, "institution_id": iid, "logical_audit_id": family_id, "input_revision": 4, "revision": 2, "business_status": "UNDER_REVIEW", "frozen_input_hash": "a" * 64, "timezone": "UTC", "period_end": date(2024, 12, 31)}
    case = {"id": case_id, "input_revision": 7, "status": "CLAIMED", "reason": "Reviewed", "assigned_reviewer": reviewer, "claim_expires_at": datetime.now(timezone.utc) + timedelta(minutes=1), "resource_type": "INSTITUTION_AUDIT", "decision": "APPROVE", "approved_snapshot_id": sid, "result_hash": "b" * 64}
    snapshot = {"id": sid, "audit_id": aid, "input_hash": "a" * 64, "result_hash": "b" * 64, "result": {"actual_total": "1"}}
    repo = MemoryRepo({"institution_audits": [audit, {"id": old_id, "business_status": "FINALIZED"}], "institution_audit_periods": [{"id": family_id, "current_final_audit_id": old_id}], "review_cases": [case], "institution_calculation_snapshots": [snapshot]})
    ctx = SimpleNamespace(repo=repo, actor={"id": reviewer}, parent=audit, case=case, iid=iid, match=lambda row: None)
    body = {"case_input_revision": 7, "snapshot_id": sid, "expected_input_hash": "a" * 64, "expected_result_hash": "c" * 64}
    with pytest.raises(InstitutionError) as error:
        run(workflow_service.finalize(ctx, body))
    assert error.value.code == "RESULT_HASH_CHANGED" and not repo.writes
    body["expected_result_hash"] = "b" * 64
    async def resource(*args):
        return args[-1]
    import services.institution.annual_service as annual
    monkeypatch.setattr(annual, "resource", resource)
    result = run(workflow_service.finalize(ctx, body))
    assert result["business_status"] == "FINALIZED"
    assert repo.tables["institution_audits"][1]["business_status"] == "SUPERSEDED"
    assert repo.tables["institution_audit_periods"][0]["current_final_audit_id"] == aid
    assert repo.tables["institution_audit_results"][0]["result_hash"] == "b" * 64


def test_privacy_cannot_delete_farmer_before_any_provider_call(monkeypatch):
    target = uid()
    frozen = {"policy_hash": "a" * 64}
    request = {"id": uid(), "status": "APPROVED", "user_id": target, "policy_version": uid(), "plan_hash": digest(frozen)}
    repo = MemoryRepo({"users": [{"id": target, "account_domain": "FARMER"}]})
    async def no_blockers(*args):
        return []
    async def policy(*args):
        return {"artifact_hash": "a" * 64}
    monkeypatch.setattr(privacy_service, "blockers", no_blockers)
    monkeypatch.setattr(privacy_service, "policy", policy)
    with pytest.raises(InstitutionError) as error:
        run(privacy_service.execute(repo, {"frozen_input": {"plan": frozen}}, request))
    assert error.value.code == "PERMISSION_DENIED" and not repo.writes


def test_retention_exception_cannot_name_an_unrelated_tenant():
    target, rid = uid(), uid()
    repo = MemoryRepo({"institution_audits": [{"id": rid, "institution_id": uid()}]})
    policy = {"config": {"categories": [{"category": "ANNUAL_AUDIT", "basis": "Approved basis", "duration_days": None, "legal_hold_rule": "Continuing review"}]}}
    item = {"resource_type": "ANNUAL_AUDIT", "id": rid, "legal_or_business_basis": "Approved basis", "retention_until": None}
    with pytest.raises(InstitutionError) as error:
        run(privacy_service.validate_retained(repo, target, policy, [item], {"purge_evidence": []}))
    assert error.value.status == 404


def test_free_tier_intent_routing_never_transmits_volunteered_identity():
    result = provider_question("I am PersonSecret at ConfidentialCampus. Call +919876543210. Explain solar at 23.1111,77.2222", ["UntrustedSecret"])
    assert "solar" in result
    assert not any(value in result for value in ("PersonSecret", "ConfidentialCampus", "9876543210", "23.1111", "UntrustedSecret"))


@pytest.mark.parametrize("revoked", [False, True])
def test_receipt_replay_is_authorized_before_stale_revision(monkeypatch, revoked):
    import sys
    import app.institution_context as module
    monkeypatch.setattr(settings, "INSTITUTION_SCHEMA_ENABLED", True)
    monkeypatch.setattr(settings, "ENABLE_INSTITUTION_FLOW", True)
    monkeypatch.setattr(settings, "ENABLE_INSTITUTION_DEMO_ACCOUNTS", False)
    monkeypatch.setattr(module, "router", APIRouter())
    actor = {"id": uid(), "account_domain": "INSTITUTION"}
    receipt = {"id": uid(), "request_hash": "placeholder", "receipt_state": "FULL", "expires_at": datetime.now(timezone.utc) + timedelta(hours=1), "secret_ciphertext": None, "accepted_resource_id": None, "http_status": 200, "response": {"data": {"saved": True}, "revision": 1, "request_id": uid(), "warnings": [], "context_id": None}}
    repo = MemoryRepo({"idempotency_records": [receipt]})
    async def find_receipt(table, filters, **kwargs):
        return [receipt] if table == "idempotency_records" else []
    repo.find = find_receipt
    class Begin:
        async def __aenter__(self):
            return repo.conn
        async def __aexit__(self, *args):
            return False
    monkeypatch.setitem(sys.modules, "app.institution_database", SimpleNamespace(_require_async_engine=lambda: SimpleNamespace(begin=lambda: Begin())))
    monkeypatch.setattr(module, "Repository", lambda conn: repo)
    async def identity(*args):
        return {"uid": uid()}
    async def user(*args):
        return actor
    async def limit(*args):
        return None
    monkeypatch.setattr(module, "identity", identity)
    monkeypatch.setattr(module, "user_for_claims", user)
    monkeypatch.setattr(Context, "rate_limit", limit)
    async def prepare(ctx):
        if revoked:
            raise InstitutionError(404, "RESOURCE_NOT_FOUND", "Unavailable")
        ctx.parent = {"input_revision": 7}
    @module.endpoint("POST", "/receipt-test", prepare=prepare)
    async def handler(ctx, body):
        ctx.match(ctx.parent)
        pytest.fail("A committed replay must not execute the handler")
    body = {"schema_version": 1}
    receipt["request_hash"] = digest({"method": "POST", "route": "/receipt-test", "query": {}, "body": body, "if_match": 1, "fixture_generation": None})
    app = FastAPI()
    app.add_exception_handler(InstitutionError, error_handler)
    app.include_router(module.router)
    with TestClient(app) as client:
        response = client.post("/receipt-test", json=body, headers={"If-Match": '"1"', "Idempotency-Key": "stable-key"})
    assert response.status_code == (404 if revoked else 200)
    if not revoked:
        assert response.headers["ETag"] == '"1"'
        assert response.json()["data"] == {"saved": True}
    assert response.headers["cache-control"].endswith("no-store")


def test_analytics_keeps_uncalculated_draft_visible_and_campus_filter_unknown(monkeypatch):
    iid, cid, aid, draft_id = uid(), uid(), uid(), uid()
    coverage = {"included_scopes": ["SCOPE_2"]}
    audit = {"id": aid, "institution_id": iid, "business_status": "FINALIZED", "period_start": date(2024, 1, 1), "period_end": date(2024, 12, 31), "timezone": "UTC", "coverage": coverage, "revision": 1, "campus_ids": [cid, uid()], "reviewed_snapshot_id": uid(), "preview_snapshot_id": None, "denominators": {"students": 10}}
    draft = {**audit, "id": draft_id, "business_status": "DRAFT", "period_start": date(2025, 1, 1), "period_end": date(2025, 12, 31), "reviewed_snapshot_id": None}
    source_id = uid()
    frozen = {"audit": audit, "emissions": [{"id": source_id, "category": "ELECTRICITY", "campus_id": None}], "tree_links": []}
    repo = MemoryRepo({"institution_audits": [audit, draft], "campuses": [{"id": cid, "institution_id": iid}], "institution_calculation_snapshots": [{"id": audit["reviewed_snapshot_id"], "frozen_input": frozen}]})
    request = Request({"type": "http", "query_string": f"campus_ids={cid}&scope=SCOPE_2".encode(), "headers": []})
    ctx = SimpleNamespace(repo=repo, iid=iid, member={"permission": "OWNER"}, request=request)
    async def result(*args):
        return {"snapshot_id": audit["reviewed_snapshot_id"], "period_start": "2024-01-01", "period_end": "2024-12-31", "timezone": "UTC", "coverage": coverage, "actual_total": "100", "by_scope": {"SCOPE_2": "100"}, "by_category": {"ELECTRICITY": "100"}, "tree_stock": "5", "removal": "1", "release": "0", "avoided_by_action": {}, "inventory_balance": "-99", "versions": {"method_version": uid(), "factor_versions": []}, "calculation_lines": [{"input_id": source_id, "included_contribution": True, "scope": "SCOPE_2", "contribution_amount": "100"}]}
    import services.institution.annual_service as annual
    monkeypatch.setattr(annual, "result", result)
    output = run(analytics_service.analytics(ctx))
    assert len(output["periods"]) == 2
    assert output["periods"][0]["actual_total"] is None
    assert output["periods"][0]["by_scope"] == {}
    assert output["periods"][1]["snapshot_id"] is None
    assert "UNCALCULATED_DRAFT" in output["periods"][1]["comparability_flags"]
    assert output["trend_delta"] is None


def test_disabled_flow_discovery_uses_released_profile_even_for_owner(monkeypatch):
    from services.institution.institution_service import institution_resource
    monkeypatch.setattr(settings, "ENABLE_INSTITUTION_FLOW", False)
    iid, actor, sid = uid(), {"id": uid()}, uid()
    current = {"id": iid, "profile": {"name": "private current draft"}}
    historical = {"id": iid, "profile": {"name": "released name"}}
    repo = MemoryRepo({"institution_users": [{"id": uid(), "institution_id": iid, "user_id": actor["id"], "status": "ACTIVE", "permission": "OWNER"}], "institution_audits": [{"id": uid(), "institution_id": iid, "business_status": "FINALIZED", "period_end": date(2024, 12, 31), "reviewed_snapshot_id": sid}], "institution_calculation_snapshots": [{"id": sid, "frozen_input": {"audit": {"boundary_snapshot": {"institution": historical}}}}]})
    result = run(institution_resource(SimpleNamespace(repo=repo, actor=actor), current))
    assert result["profile"] == historical["profile"]
    assert result["permission"] == "VIEWER"


def test_viewer_campus_list_honors_exact_snapshot_without_tree_assessments():
    from routers.campuses import list_campuses
    iid, sid, aid, cid = uid(), uid(), uid(), uid()
    frozen = {"audit": {"boundary_snapshot": {"campuses": [{"id": cid, "name": "historical campus", "control_evidence_ids": [uid()], "control_review_case_id": uid()}]}}, "tree_links": []}
    repo = MemoryRepo({"institution_calculation_snapshots": [{"id": sid, "audit_id": aid, "institution_id": iid, "frozen_input": frozen}], "institution_audits": [{"id": aid, "business_status": "FINALIZED", "reviewed_snapshot_id": sid}]})
    async def page(rows, *args):
        return {"items": rows}
    ctx = SimpleNamespace(repo=repo, iid=iid, member={"permission": "VIEWER"}, request=Request({"type": "http", "query_string": f"snapshot_id={sid}".encode(), "headers": []}), page_rows=page)
    item = run(list_campuses(ctx, None))["items"][0]
    assert item["id"] == cid and item["name"] == "historical campus"
    assert item["eligible_area_ha"] is None and item["latest_accepted_tree_audit_id"] is None
    assert item["control_evidence_ids"] == [] and item["control_review_case_id"] is None


@pytest.mark.parametrize("is_latest", [True, False])
def test_failure_recovery_obeys_lock_order_and_never_overwrites_new_job(monkeypatch, is_latest):
    from tasks import institution_tasks as worker
    iid, jid, rid = uid(), uid(), uid()
    job = {"id": jid, "institution_id": iid, "job_type": "campus_mask", "resource_id": rid, "status": "FAILED", "retryable": False, "error": {"code": "JOB_LEASE_EXPIRED"}, "stage": "RECOVERY"}
    locks = []
    class RecoveryRepo(MemoryRepo):
        async def get(self, table, row_id, **kwargs):
            if kwargs.get("lock"):
                locks.append(table)
            return await super().get(table, row_id, **kwargs)
        async def one(self, *args, **kwargs):
            return {"id": jid if is_latest else uid()}
    repo = RecoveryRepo({"institutions": [{"id": iid}], "processing_jobs": [job], "campus_vegetation_masks": [{"id": rid, "processing_status": "RUNNING"}]})
    class Begin:
        async def __aenter__(self):
            return repo.conn
        async def __aexit__(self, *args):
            return False
    monkeypatch.setattr(worker, "Repository", lambda conn: repo)
    run(worker.reconcile_failure(SimpleNamespace(begin=lambda: Begin()), jid))
    assert locks == ["institutions", "campus_vegetation_masks", "processing_jobs"]
    assert repo.tables["campus_vegetation_masks"][0]["processing_status"] == ("FAILED" if is_latest else "RUNNING")
    assert job["stage"] == "FAILURE_RECONCILED"
