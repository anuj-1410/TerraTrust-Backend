"""Shared FastAPI dependencies used across the backend routers."""

import logging
from typing import Any, Dict, Optional

from fastapi import Header, HTTPException, status

from app.database import supabase_client
from app.firebase_auth import verify_firebase_token

logger = logging.getLogger("terratrust.dependencies")

USER_SELECT = "id, firebase_uid, phone_number, full_name, kyc_completed, wallet_address"


def _attach_wallet_recovery_state(user: Dict[str, Any]) -> Dict[str, Any]:
    """Attach the latest wallet-recovery request status for UI bootstrap flows."""
    enriched_user = dict(user)
    enriched_user.setdefault("wallet_recovery_status", None)
    enriched_user.setdefault("wallet_recovery_requested_at", None)

    try:
        response = (
            supabase_client.table("wallet_recovery_requests")
            .select("status, requested_at")
            .eq("user_id", user["id"])
            .order("requested_at", desc=True)
            .limit(1)
            .execute()
        )
        latest_request = response.data[0] if response.data else None
        if latest_request:
            enriched_user["wallet_recovery_status"] = latest_request.get("status")
            enriched_user["wallet_recovery_requested_at"] = latest_request.get("requested_at")
    except Exception as exc:
        logger.warning(
            "Failed to load wallet recovery state for user %s: %s",
            user.get("id"),
            exc,
        )

    return enriched_user


def _extract_phone_number(decoded_token: Dict[str, Any]) -> Optional[str]:
    """Extract the Firebase-authenticated phone number from decoded claims."""
    phone_number = decoded_token.get("phone_number")
    if phone_number:
        return phone_number

    firebase_claims = decoded_token.get("firebase", {})
    identities = firebase_claims.get("identities", {})
    phone_identities = identities.get("phoneNumber") or identities.get("phone_number")
    if isinstance(phone_identities, list) and phone_identities:
        return str(phone_identities[0])
    if isinstance(phone_identities, str):
        return phone_identities
    return None


def _fetch_user_by(field: str, value: str) -> Optional[Dict[str, Any]]:
    """Return the first user row matching a field/value pair."""
    response = (
        supabase_client.table("users")
        .select(USER_SELECT)
        .eq(field, value)
        .limit(1)
        .execute()
    )
    return _attach_wallet_recovery_state(response.data[0]) if response.data else None


def _refresh_user_record(user_id: str) -> Dict[str, Any]:
    """Re-read the canonical user row after a write operation."""
    user = _fetch_user_by("id", user_id)
    if user is None:
        raise RuntimeError(f"Backend user '{user_id}' could not be reloaded after provisioning.")
    return user


def _provision_user(firebase_uid: str, phone_number: str) -> Dict[str, Any]:
    """Upsert a backend user row for the authenticated Firebase identity."""
    user = _fetch_user_by("firebase_uid", firebase_uid)
    if user:
        if user.get("phone_number") != phone_number:
            supabase_client.table("users").update({"phone_number": phone_number}).eq(
                "id", user["id"]
            ).execute()
            return _refresh_user_record(user["id"])
        return user

    user = _fetch_user_by("phone_number", phone_number)
    if user:
        supabase_client.table("users").update(
            {
                "firebase_uid": firebase_uid,
                "phone_number": phone_number,
            }
        ).eq("id", user["id"]).execute()
        return _refresh_user_record(user["id"])

    try:
        supabase_client.table("users").insert(
            {
                "firebase_uid": firebase_uid,
                "phone_number": phone_number,
                "kyc_completed": False,
            }
        ).execute()
    except Exception as exc:
        existing_user = _fetch_user_by("firebase_uid", firebase_uid) or _fetch_user_by(
            "phone_number", phone_number
        )
        if existing_user is not None:
            return existing_user
        raise RuntimeError("Failed to provision backend user profile.") from exc

    created_user = _fetch_user_by("firebase_uid", firebase_uid) or _fetch_user_by(
        "phone_number", phone_number
    )
    if created_user is not None:
        return created_user
    raise RuntimeError("Failed to provision backend user profile.")


def get_current_user(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    """Verify a Firebase ID token and return the provisioned backend user row."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header. Expected 'Bearer <token>'.",
        )

    token = authorization.removeprefix("Bearer ").strip()

    try:
        decoded_token = verify_firebase_token(token)
    except Exception as exc:
        logger.warning("Firebase token verification failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token.",
        ) from exc

    firebase_uid = decoded_token.get("uid")
    if not firebase_uid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token is missing a Firebase uid.",
        )

    phone_number = _extract_phone_number(decoded_token)
    if not phone_number:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token is missing the verified phone number.",
        )

    try:
        return _provision_user(firebase_uid=firebase_uid, phone_number=phone_number)
    except Exception as exc:
        logger.error("User provisioning failed for Firebase uid %s: %s", firebase_uid, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authenticated user profile is temporarily unavailable.",
        ) from exc
