"""Policy-bound institution privacy requests; actor UUIDs survive lawful retention."""
import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

from firebase_admin import auth
from sqlalchemy import text

from app.config import settings
from app.firebase_auth import get_firebase_app
from app.institution_access import recent_auth
from app.institution_errors import fail
from models.institution.common import digest, normalize
from services.institution.job_service import accepted, enqueue
from app.institution_rules import retention_rules


async def policy(repo, policy_id=None):
    current = retention_rules(settings.INSTITUTION_PRIVACY_POLICY_URL)
    if policy_id is None or str(policy_id) == current["id"]:
        return current
    # Preserve previously accepted plans after a code/configuration change.
    row = await repo.get("institution_deployment_policies", policy_id, required=False)
    if row is None:
        saved = await repo.one("SELECT resolution->'rules' AS rules FROM institution_deletion_requests WHERE policy_version=:id AND resolution ? 'rules' ORDER BY created_at DESC LIMIT 1", {"id": policy_id}, required=False)
        row = saved["rules"] if saved else None
        if not row or row.get("artifact_hash") != digest(row.get("config", {})) or str(row.get("id")) != str(policy_id):
            fail(409, "PRIVACY_PLAN_CHANGED", "Refresh and review this request under the current application deletion rules.")
    conf = row["config"]
    if row["kind"] != "RETENTION" or row["status"] not in {"ACTIVE", "APPROVED"} or not conf.get("categories") or not conf.get("backup_expiry_rule") or not conf.get("provider_rules"):
        fail(503, "RETENTION_POLICY_NOT_CONFIGURED", "A complete independently approved retention policy is required.")
    return row


async def blockers(repo, uid):
    owners = await repo.rows("SELECT iu.institution_id FROM institution_users iu WHERE iu.user_id=:uid AND iu.status='ACTIVE' AND iu.permission='OWNER' AND NOT EXISTS(SELECT 1 FROM institution_users other WHERE other.institution_id=iu.institution_id AND other.user_id<>iu.user_id AND other.status='ACTIVE' AND other.permission='OWNER')", {"uid": uid})
    return [{"type": "OWNER_TRANSFER", "description": "Transfer ownership of institution " + str(row["institution_id"]) + " to another verified active owner."} for row in owners]


async def plan(repo, uid, approved_policy):
    evidence = await repo.find("institution_audit_evidence", {"owner_user_id": uid})
    purge = []
    for item in evidence:
        links = await repo.find("institution_evidence_links", {"evidence_id": item["id"]})
        if item["parent_type"] == "REGISTRATION_DRAFT" and not links and not item["bytes_deleted_at"]:
            purge.append({"id": str(item["id"]), "bucket": item["private_bucket"], "object_key": item["object_key"], "byte_hash": item["byte_hash"] or item["expected_sha256"]})
    rule = next((r for r in approved_policy["config"]["categories"] if r["category"] == "UNBOUND_PERSONAL_UPLOADS"), None)
    if purge and (not rule or rule["deletion_rule"] != "DELETE_ON_VERIFIED_ACCOUNT_DELETION"):
        fail(503, "RETENTION_PLAN_INCOMPLETE", "The approved policy must resolve unbound personal uploads explicitly.")
    provider = approved_policy["config"]["provider_rules"]
    if provider.get("firebase") != "DELETE_IDENTITY" or provider.get("gemini") != "NO_PRIVATE_DATA_TRANSMISSION":
        fail(503, "RETENTION_PLAN_INCOMPLETE", "Commission explicit Firebase and redacted Gemini handling.")
    queries = await repo.find("advisory_queries", {"user_id":uid})
    personal_rule = next((r for r in approved_policy["config"]["categories"] if r["category"] == "PERSONAL_ADVISOR_TEXT"),None)
    if queries and (not personal_rule or personal_rule["deletion_rule"] != "DELETE_ON_VERIFIED_ACCOUNT_DELETION"):
        fail(503,"RETENTION_PLAN_INCOMPLETE","The reviewed policy must resolve personal advisor text explicitly.")
    return {"schema_version": 1, "user_id": str(uid), "policy_version": str(approved_policy["id"]), "policy_hash": approved_policy["artifact_hash"], "purge_evidence": purge, "redact_advisory_ids":sorted(str(q["id"]) for q in queries), "revoke_memberships": True, "firebase": "DELETE_IDENTITY", "personal_fields": ["firebase_uid", "phone_number", "full_name"], "retained_categories": approved_policy["config"]["categories"], "backup_expiry_rule": approved_policy["config"]["backup_expiry_rule"]}


async def validate_retained(repo, uid, selected, items, frozen):
    """Exceptions identify authorized records and reproduce the approved basis."""
    tables = {"EVIDENCE": "institution_audit_evidence", "ANNUAL_AUDIT": "institution_audits", "ANNUAL_RESULT": "institution_audit_results", "TREE_AUDIT": "campus_tree_audits", "REVIEW_CASE": "review_cases"}
    memberships = await repo.find("institution_users", {"user_id": uid})
    created = await repo.find("institutions", {"created_by": uid})
    tenants = {str(row["institution_id"]) for row in memberships} | {str(row["id"]) for row in created}
    rules = {rule["category"]: rule for rule in selected["config"]["categories"]}
    purge = {item["id"] for item in frozen["purge_evidence"]}
    seen = set()
    for item in items:
        category, rid = item["resource_type"], item["id"]
        rule = rules.get(category)
        if (category, rid) in seen or not rule or category not in tables or rid in purge:
            fail(422, "INVALID_RETENTION_EXCEPTION", "Select unique shared records covered by the approved retention policy.")
        seen.add((category, rid))
        row = await repo.get(tables[category], rid)
        if str(row.get("institution_id")) not in tenants and not (category == "EVIDENCE" and str(row.get("owner_user_id")) == str(uid)):
            fail(404, "RESOURCE_NOT_FOUND", "Retained record is outside this privacy request.")
        if item["legal_or_business_basis"] != rule["basis"]:
            fail(422, "RETENTION_BASIS_MISMATCH", "The disclosed retention basis must equal the approved policy.")
        expected = row["created_at"] + timedelta(days=rule["duration_days"]) if rule["duration_days"] is not None else None
        supplied = datetime.fromisoformat(item["retention_until"].replace("Z", "+00:00")) if item["retention_until"] else None
        if supplied != expected or expected is None and not rule["legal_hold_rule"]:
            fail(422, "RETENTION_DEADLINE_MISMATCH", "Use the exact policy deadline or its documented continuing hold basis.")


def resource(row):
    resolution = row["resolution"]
    return {"id": str(row["id"]), "input_revision": row["input_revision"], "status": row["status"], "created_at": row["created_at"], "required_actions": resolution.get("required_actions", []), "retention_summary": resolution.get("retention_summary", "The approved retention policy governs shared evidence, actor references and backups."), "support_reference": "TT-PRIVACY-" + str(row["id"])}


async def create(ctx, body):
    recent_auth(ctx.claims)
    selected = await policy(ctx.repo)
    remaining = await blockers(ctx.repo, ctx.actor["id"])
    frozen = await plan(ctx.repo, ctx.actor["id"], selected)
    row = await ctx.repo.insert("institution_deletion_requests", {"user_id": ctx.actor["id"], "status": "NEEDS_ACTION" if remaining else "REQUESTED", "reason": body["reason"], "request_hash": digest(body), "policy_version": selected["id"], "plan_hash": digest(frozen), "verified_at": datetime.now(timezone.utc), "resolution": {"required_actions": remaining, "plan": frozen, "rules": normalize(selected), "retention_summary": "Application deletion rules " + selected["logical_key"] + " version " + str(selected["version"]) + ": " + selected["config"]["approved_policy_url"]}})
    await ctx.repo.event(ctx.actor, "PRIVACY_REQUESTED", "institution_deletion_requests", row)
    ctx.revision = row["input_revision"]
    return resource(row)


async def execute(repo, job, request):
    if request["status"] == "COMPLETED":
        return request["id"]
    if request["status"] not in {"APPROVED", "PROCESSING", "FAILED_RETRYABLE"} or await blockers(repo, request["user_id"]):
        fail(409, "PRIVACY_OWNERSHIP_UNRESOLVED", "Ownership or privacy authorization changed before execution.")
    selected = await policy(repo, request["policy_version"])
    frozen = job["frozen_input"]["plan"]
    if digest(frozen) != request["plan_hash"] or selected["artifact_hash"] != frozen["policy_hash"]:
        fail(409, "PRIVACY_PLAN_CHANGED", "The reviewed privacy plan changed.")
    user = await repo.one("SELECT * FROM users WHERE id=:uid FOR UPDATE", {"uid": request["user_id"]})
    if user["account_domain"] != "INSTITUTION":
        fail(403, "PERMISSION_DENIED", "Institution deletion cannot touch a farmer identity.")
    memberships = await repo.find("institution_users",{"user_id":user["id"]})
    for iid in sorted({str(member["institution_id"]) for member in memberships}):
        await repo.get("institutions",iid,lock=True)
    if await blockers(repo,user["id"]) or digest(await plan(repo,user["id"],selected)) != request["plan_hash"]:
        fail(409,"PRIVACY_PLAN_CHANGED","Account content or ownership changed; independently review the current plan before deletion.")
    # Revoke operational authority before provider actions. Jobs that already
    # hold a grant finish their transaction first; subsequent publication must
    # recheck the now-revoked grant after this transaction commits.
    await repo.conn.execute(text("UPDATE operator_grants SET revoked_at=now(),updated_at=now() WHERE user_id=:uid AND revoked_at IS NULL"), {"uid": user["id"]})
    await repo.update("institution_deletion_requests", request["id"], {"status": "PROCESSING"})
    from app.database import supabase_client
    for item in frozen["purge_evidence"]:
        await execution_lease(repo, job)
        actual = await repo.get("institution_audit_evidence", item["id"], lock=True)
        if str(actual["owner_user_id"]) != str(user["id"]) or actual["private_bucket"] != item["bucket"] or actual["object_key"] != item["object_key"] or await repo.find("institution_evidence_links", {"evidence_id": actual["id"]}):
            fail(409, "PRIVACY_PLAN_CHANGED", "Private upload scope changed; independently review a new plan.")
        try:
            await asyncio.to_thread(supabase_client.storage.from_(item["bucket"]).remove, [item["object_key"]])
            # Confirm absence with the storage API; never interpret an arbitrary
            # exception (including an outage) as a successful delete.
            parent, name = item["object_key"].rsplit("/", 1)
            entries = await asyncio.to_thread(supabase_client.storage.from_(item["bucket"]).list, parent, {"search": name, "limit": 100})
            if any(entry.get("name") == name for entry in entries):
                raise ValueError()
        except Exception:
            fail(503, "PRIVACY_STORAGE_UNCONFIRMED", "Storage deletion has not been confirmed.", retryable=True)
        await repo.update("institution_audit_evidence", actual["id"], {"bytes_deleted_at": datetime.now(timezone.utc), "purge_policy_version": selected["id"]})
    firebase_uid = user["firebase_uid"]
    if firebase_uid:
        await execution_lease(repo, job)
        try:
            await asyncio.to_thread(auth.revoke_refresh_tokens, firebase_uid, app=get_firebase_app())
            await asyncio.to_thread(auth.delete_user, firebase_uid, app=get_firebase_app())
        except auth.UserNotFoundError:
            pass
        except Exception:
            fail(503, "PRIVACY_IDENTITY_UNCONFIRMED", "Firebase identity deletion has not been confirmed.", retryable=True)
        try:
            await asyncio.to_thread(auth.get_user, firebase_uid, app=get_firebase_app())
        except auth.UserNotFoundError:
            pass
        except Exception:
            fail(503, "PRIVACY_IDENTITY_UNCONFIRMED", "Firebase identity deletion has not been confirmed.", retryable=True)
        else:
            fail(503, "PRIVACY_IDENTITY_UNCONFIRMED", "Firebase identity still exists.", retryable=True)
    await repo.conn.execute(text("UPDATE institution_users SET status='REVOKED',revoked_at=now(),updated_at=now() WHERE user_id=:uid AND status<>'REVOKED'"), {"uid": user["id"]})
    await repo.conn.execute(text("DELETE FROM institution_notifications WHERE recipient_user_id=:uid"), {"uid": user["id"]})
    await repo.conn.execute(text("DELETE FROM advisory_feedback WHERE user_id=:uid"), {"uid":user["id"]})
    await repo.conn.execute(text("UPDATE advisory_queries SET question='[Deleted under approved policy]',constraints='{}'::jsonb,publication_state='WITHDRAWN',updated_at=now() WHERE user_id=:uid AND id=ANY(CAST(:ids AS uuid[]))"), {"uid":user["id"],"ids":frozen["redact_advisory_ids"]})
    await repo.conn.execute(text("UPDATE processing_jobs SET frozen_input=jsonb_set(frozen_input,'{question}',to_jsonb(CAST(:redacted AS text)),false),updated_at=now() WHERE domain='INSTITUTION' AND job_type='advisory' AND requested_by=:uid AND resource_id=ANY(CAST(:ids AS uuid[]))"), {"uid":user["id"], "ids":frozen["redact_advisory_ids"], "redacted":"[Deleted under approved policy]"})
    await repo.conn.execute(text("UPDATE users SET institution_deleted_uid_hash=:hash,firebase_uid=NULL,phone_number=NULL,full_name=NULL,updated_at=now() WHERE id=:id AND account_domain='INSTITUTION'"), {"id": user["id"], "hash": hashlib.sha256(firebase_uid.encode()).hexdigest() if firebase_uid else user["institution_deleted_uid_hash"]})
    resolution = {**request["resolution"], "provider_confirmations": {"firebase": "IDENTITY_ABSENT", "storage": "SCOPED_OBJECTS_ABSENT", "gemini": "PRIVATE_DATA_NOT_TRANSMITTED"}, "completed_at": normalize(datetime.now(timezone.utc))}
    await repo.update("institution_deletion_requests", request["id"], {"status": "COMPLETED", "completed_at": datetime.now(timezone.utc), "resolution": resolution})
    return request["id"]


async def execution_lease(repo, job):
    """Check current ownership before each irreversible provider action."""
    active = await repo.one("SELECT id FROM processing_jobs WHERE id=:id AND status='RUNNING' AND lease_owner=:owner AND lease_expires_at>now() AND deadline_at>now()", {"id": job["id"], "owner": job["lease_owner"]}, required=False)
    if not active:
        fail(409, "JOB_LEASE_LOST", "Privacy execution no longer owns the accepted operation.")
