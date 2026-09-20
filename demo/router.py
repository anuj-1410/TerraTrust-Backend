"""Development-only API endpoints for demo-account management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.dependencies import get_current_user
from demo.config import (
    DEMO_FIREBASE_UIDS,
    DEMO_UID_PLACEHOLDER,
    ENABLE_DEMO_ACCOUNTS,
    get_demo_account,
    get_demo_status_accounts,
    is_demo_uid,
    is_resettable_demo,
)
from demo.middleware import invalidate_demo_session
from demo.restore import restore_to_checkpoint

router = APIRouter(tags=["Demo - Development Only"])


@router.post("/reset/{phone_suffix}")
async def manually_reset_demo_account(
    phone_suffix: str,
    current_user: dict = Depends(get_current_user),
):
    """Manually reset the authenticated resettable demo account to its checkpoint state."""
    phone = phone_suffix if phone_suffix.startswith("+") else f"+91{phone_suffix}"
    firebase_uid = DEMO_FIREBASE_UIDS.get(phone)

    if not firebase_uid or firebase_uid == DEMO_UID_PLACEHOLDER:
        return {"error": f"{phone} is not a registered demo account or UIDs not yet configured"}

    current_uid = str(current_user.get("firebase_uid") or "")
    if current_uid != firebase_uid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Demo reset is only allowed for the authenticated demo account.",
        )

    if not is_resettable_demo(firebase_uid):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This demo account is persistent and cannot be reset.",
        )

    invalidate_demo_session(firebase_uid)
    restored = await restore_to_checkpoint(firebase_uid)
    if not restored:
        return {
            "error": "reset_failed",
            "phone": phone,
        }

    config = get_demo_account(firebase_uid) or {}
    return {
        "status": "reset_complete",
        "phone": phone,
        "checkpoint": config.get("checkpoint"),
        "persistent": config.get("persistent"),
    }


@router.get("/status")
async def demo_status(current_user: dict = Depends(get_current_user)):
    """Return demo-account configuration to authenticated demo users only."""
    current_uid = str(current_user.get("firebase_uid") or "")
    if not is_demo_uid(current_uid):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Demo status is only available to configured demo accounts.",
        )

    return {
        "enabled": ENABLE_DEMO_ACCOUNTS,
        "accounts": get_demo_status_accounts(),
    }
