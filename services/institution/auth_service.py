"""Transactional account-domain provisioning and additive flat bootstrap."""
from datetime import datetime, timedelta, timezone
import asyncio
import hashlib
from uuid import uuid4

from sqlalchemy import text

from app.config import settings
from app.institution_access import identity
from app.institution_errors import fail
from models.institution.common import digest, normalize
from repositories.institution.base import Repository


async def bootstrap(authorization, selected):
    if selected not in (None, "FARMER", "INSTITUTION"):
        fail(400, "INVALID_LOGIN_FLOW", "Choose FARMER or INSTITUTION.")
    if not settings.INSTITUTION_SCHEMA_ENABLED:
        if selected == "INSTITUTION":
            fail(503, "INSTITUTION_FLOW_DISABLED", "Institution access is not available yet.")
        from app.dependencies import get_current_user
        from routers.auth import _build_auth_me_response
        def legacy_bootstrap():
            return _build_auth_me_response(get_current_user(authorization)).model_dump()
        return await asyncio.to_thread(legacy_bootstrap)
    claims = await identity(authorization)
    demo_session = None
    if settings.ENABLE_INSTITUTION_DEMO_ACCOUNTS and selected != "FARMER":
        from demo.institution.sessions import ensure_login
        demo_session = await ensure_login(claims)
        if demo_session and demo_session["status"] != "READY":
            fail(409, "DEMO_SESSION_NOT_READY", "The next-login checkpoint is being restored. Retry bootstrap after polling the session.", retryable=True, details={"session_id": demo_session["session_id"], "status": demo_session["status"], "poll_after_seconds": 2})
    from app.institution_database import _require_async_engine
    async with _require_async_engine().begin() as conn:
        repo = Repository(conn)
        await conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:uid,0))"), {"uid": "tt:identity:" + claims["uid"]})
        user = await repo.one("SELECT * FROM users WHERE firebase_uid=:uid", {"uid": claims["uid"]}, required=False)
        if user and user["account_domain"] == "INSTITUTION":
            user = await repo.one("SELECT * FROM users WHERE firebase_uid=:uid FOR UPDATE", {"uid": claims["uid"]}, required=False)
            if not user:
                fail(410, "ACCOUNT_DELETED", "This institution identity was deleted.")
        deleted = await repo.one("SELECT id FROM users WHERE institution_deleted_uid_hash=:hash", {"hash": hashlib.sha256(claims["uid"].encode()).hexdigest()}, required=False)
        if deleted:
            fail(410, "ACCOUNT_DELETED", "This institution identity was deleted.")
        if not user:
            if selected == "INSTITUTION" and not settings.ENABLE_INSTITUTION_FLOW:
                fail(503, "INSTITUTION_FLOW_DISABLED", "New institution identities are disabled.")
            conflict = await repo.one("SELECT id FROM users WHERE phone_number=:phone", {"phone": claims["phone_number"]}, required=False)
            if conflict:
                fail(409, "IDENTITY_CONFLICT", "This phone identity is already linked to a different user.")
            domain = selected or "FARMER"
            await conn.execute(text("INSERT INTO users(id,firebase_uid,phone_number,role,account_domain,institution_onboarding_status,kyc_completed) VALUES(:id,:uid,:phone,:role,:domain,:onboarding,false)"), {"id": str(uuid4()), "uid": claims["uid"], "phone": claims["phone_number"], "role": "INSTITUTION_ADMIN" if domain == "INSTITUTION" else "FARMER", "domain": domain, "onboarding": "REQUIRED" if domain == "INSTITUTION" else None})
            user = await repo.one("SELECT * FROM users WHERE firebase_uid=:uid", {"uid": claims["uid"]})
        if selected and user["account_domain"] != selected:
            fail(409, "ACCOUNT_DOMAIN_MISMATCH", "Choose the account's existing flow.", details={"expected_domain": user["account_domain"], "selected_domain": selected})
        if user["phone_number"] != claims["phone_number"]:
            fail(409, "IDENTITY_CONFLICT", "Phone identity changed; contact support.")
        result = {"user_id": str(user["id"]), "firebase_uid": user["firebase_uid"], "phone_number": user["phone_number"], "full_name": user["full_name"], "kyc_completed": bool(user["kyc_completed"]), "wallet_address": user["wallet_address"], "wallet_recovery_status": None, "wallet_recovery_requested_at": None, "account_domain": user["account_domain"], "role": user["role"], "institution_onboarding_status": user["institution_onboarding_status"], "institution_memberships": [], "registration_id": None, "registration_institution_id": None, "registration_business_status": None, "registration_authorizations": [], "institution_offline_context": None, "institution_links": {"privacy": settings.INSTITUTION_PRIVACY_POLICY_URL or None, "support": settings.INSTITUTION_SUPPORT_URL or None, "account_deletion": settings.INSTITUTION_ACCOUNT_DELETION_URL or None}, "capabilities": {"institution_flow": settings.ENABLE_INSTITUTION_FLOW, "institution_registration": settings.ENABLE_INSTITUTION_FLOW, "institution_read": settings.INSTITUTION_SCHEMA_ENABLED, "institution_advisor": settings.ENABLE_INSTITUTION_FLOW and settings.ENABLE_INSTITUTION_ADVISOR}}
        if user["account_domain"] == "FARMER":
            recovery = await repo.one("SELECT status,requested_at FROM wallet_recovery_requests WHERE user_id=:uid ORDER BY requested_at DESC LIMIT 1", {"uid": user["id"]}, required=False)
            if recovery:
                result.update(wallet_recovery_status=recovery["status"], wallet_recovery_requested_at=normalize(recovery["requested_at"]))
            return result
        flow_ready = settings.ENABLE_INSTITUTION_FLOW and not settings.institution_missing_config()
        result["capabilities"].update(institution_flow=flow_ready, institution_registration=flow_ready, institution_advisor=flow_ready and settings.ENABLE_INSTITUTION_ADVISOR and not settings.institution_missing_config(advisor=True))
        if demo_session:
            result["demo_context"] = {key: demo_session[key] for key in ("account_number", "checkpoint_key", "fixture_generation", "reset_policy", "audit_demo_mode")}
        members = await repo.rows("SELECT iu.*, i.profile,i.verification_status FROM institution_users iu JOIN institutions i ON i.id=iu.institution_id WHERE iu.user_id=:uid ORDER BY iu.created_at DESC", {"uid": user["id"]})
        result["institution_memberships"] = [{"institution_id": str(m["institution_id"]), "name": m["profile"]["name"], "permission": m["permission"], "membership_status": m["status"], "institution_status": m["verification_status"]} for m in members]
        registration = await repo.one("SELECT r.* FROM institutions i JOIN institution_registrations r ON r.id=i.current_registration_id WHERE i.created_by=:uid ORDER BY i.created_at DESC LIMIT 1", {"uid": user["id"]}, required=False)
        completed = user["institution_onboarding_completed_at"] or any(m.get("joined_at") is not None or m["status"] == "ACTIVE" and m["verification_status"] == "APPROVED" for m in members)
        state = "COMPLETE" if completed else "PENDING_REVIEW" if registration and registration["status"] in {"SUBMITTED", "UNDER_REVIEW"} else "REJECTED" if registration and registration["status"] in {"REJECTED", "NEEDS_CORRECTION"} else "REQUIRED"
        result["institution_onboarding_status"] = state
        if registration:
            result.update(registration_id=str(registration["id"]), registration_institution_id=str(registration["institution_id"]), registration_business_status=registration["status"])
        grants = await repo.rows("SELECT id,expires_at FROM institution_registration_authorizations WHERE target_user_id=:uid AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at>now()", {"uid": user["id"]})
        result["registration_authorizations"] = normalize(grants)
        checked = datetime.now(timezone.utc)
        grant = {"checked_at": normalize(checked), "expires_at": normalize(checked + timedelta(seconds=settings.INSTITUTION_OFFLINE_MAX_SECONDS))}
        result["institution_offline_context"] = {**grant, "context_hash": digest({**grant, "uid": claims["uid"], "memberships": result["institution_memberships"]})}
        if state == "COMPLETE" and not user["institution_onboarding_completed_at"]:
            await conn.execute(text("UPDATE users SET institution_onboarding_status='COMPLETE',institution_onboarding_completed_at=now() WHERE id=:uid"), {"uid": user["id"]})
        return result
