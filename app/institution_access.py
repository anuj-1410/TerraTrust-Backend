"""Fresh Firebase identity, tenant ancestry and independent operator grants."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from firebase_admin import auth

from app.config import settings
from app.firebase_auth import get_firebase_app
from app.institution_errors import fail

CASE_CAPABILITIES = {"REGISTRATION": "AUTHORITY_REVIEW", "CAMPUS_CONTROL": "CAMPUS_CONTROL_REVIEW", "BOUNDARY": "BOUNDARY_REVIEW", "MASK": "MASK_REVIEW", "PLOT_SET": "PLOT_REVIEW", "SURVEY": "SURVEY_REVIEW", "TREE_AUDIT": "TREE_RESULT_REVIEW", "INSTITUTION_AUDIT": "ACCOUNTING_REVIEW"}
REGISTRY_CAPABILITIES = {"FACTOR": "FACTOR_REVIEW", "METHOD": "METHOD_REVIEW", "MODEL": "MODEL_REVIEW", "KNOWLEDGE": "KNOWLEDGE_REVIEW"}
RELEASED = {"FINALIZED", "SUPERSEDED"}


async def identity(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        fail(401, "TOKEN_EXPIRED", "A verified Firebase session is required.")
    try:
        claims = await asyncio.to_thread(auth.verify_id_token, authorization[7:].strip(), app=get_firebase_app(), check_revoked=True)
    except (auth.RevokedIdTokenError, auth.UserDisabledError):
        fail(401, "SESSION_REVOKED", "This session has been revoked.")
    except Exception:
        fail(401, "TOKEN_EXPIRED", "Authentication is unavailable or expired.")
    if not claims.get("uid") or not claims.get("phone_number"):
        fail(401, "TOKEN_EXPIRED", "Verified phone authentication is required.")
    return claims


def require_flow(*, advisor=False):
    if not settings.INSTITUTION_SCHEMA_ENABLED or not settings.ENABLE_INSTITUTION_FLOW:
        fail(503, "INSTITUTION_FLOW_DISABLED", "New institution work is disabled.")
    if advisor and not settings.ENABLE_INSTITUTION_ADVISOR:
        fail(503, "INSTITUTION_ADVISOR_DISABLED", "New advisory work is disabled.")
    if settings.institution_missing_config(advisor=advisor):
        fail(503, "METHOD_NOT_CONFIGURED", "Institution service commissioning is incomplete.")


async def user_for_claims(repo, claims, lock=None):
    if lock not in {None, "SHARE", "UPDATE"}:
        raise ValueError("Unsupported identity lock mode.")
    suffix = " FOR " + lock if lock else ""
    user = await repo.one("SELECT * FROM users WHERE firebase_uid=:uid" + suffix, {"uid": claims["uid"]}, required=False)
    if not user:
        fail(403, "PERMISSION_DENIED", "Use an institution account.")
    if user.get("account_domain") != "INSTITUTION":
        grants = await repo.rows("SELECT id FROM operator_grants WHERE user_id=:uid AND revoked_at IS NULL AND valid_from<=now() AND valid_until>now() LIMIT 1", {"uid": user["id"]})
        if not grants:
            fail(403, "PERMISSION_DENIED", "Use an institution account or an explicitly scoped operator identity.")
    if user["phone_number"] != claims["phone_number"]:
        fail(409, "IDENTITY_CONFLICT", "Verified identity differs from the saved profile.")
    return user


async def tenant_access(repo, actor, iid, mode="R", *, own_pending=False, lock=False):
    institution = await repo.get("institutions", iid, lock=lock)
    member = await repo.one("SELECT * FROM institution_users WHERE institution_id=:iid AND user_id=:uid", {"iid": iid, "uid": actor["id"]}, required=False)
    creator = str(institution["created_by"]) == str(actor["id"])
    if own_pending and creator and institution["verification_status"] in {"PENDING", "REJECTED"}:
        return institution, member
    if not member or member["status"] != "ACTIVE":
        fail(404, "RESOURCE_NOT_FOUND", "Resource is unavailable.")
    if institution["verification_status"] != "APPROVED":
        fail(403, "MEMBERSHIP_REVOKED", "Institution access is restricted.")
    if mode == "O" and member["permission"] != "OWNER":
        fail(403, "PERMISSION_DENIED", "An active owner is required.")
    if mode == "W" and member["permission"] not in {"OWNER", "MANAGER"}:
        fail(403, "PERMISSION_DENIED", "Operational write permission is required.")
    return institution, member


async def operator_access(repo, actor, capability, *, iid=None, case=None):
    grants = await repo.rows("SELECT * FROM operator_grants WHERE user_id=:uid AND capability=:cap AND revoked_at IS NULL AND valid_from<=now() AND valid_until>now() FOR SHARE", {"uid": actor["id"], "cap": capability})
    for grant in grants:
        if iid is None and case is None and grant["institution_id"] is None and grant["case_id"] is None:
            return grant
        if iid is not None and str(grant["institution_id"]) == str(iid) and grant["case_id"] is None:
            return grant
        if case and str(grant["case_id"]) == str(case["id"]):
            return grant
    fail(404, "RESOURCE_NOT_FOUND", "Resource is unavailable.")


async def independent_reviewer(repo, actor, case):
    await operator_access(repo, actor, CASE_CAPABILITIES[case["resource_type"]], iid=case["institution_id"], case=case)
    snapshot = case["resource_snapshot"]
    conflicts = set(map(str, snapshot.get("contributor_user_ids", []) + snapshot.get("historical_member_user_ids", [])))
    current = await repo.find("institution_users", {"institution_id": case["institution_id"], "user_id": actor["id"]})
    if str(actor["id"]) in conflicts or current:
        fail(403, "REVIEWER_CONFLICT", "An independent reviewer is required.")


def recent_auth(claims):
    age = datetime.now(timezone.utc).timestamp() - claims.get("auth_time", 0)
    provider = claims.get("firebase", {}).get("sign_in_provider")
    if age < -300 or age > 300 or provider != "phone":
        fail(401, "RECENT_AUTH_REQUIRED", "Verify this same phone identity again before continuing.")
