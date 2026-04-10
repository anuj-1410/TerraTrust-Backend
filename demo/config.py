"""Configuration for development-only demo accounts."""

from __future__ import annotations

import os

ENABLE_DEMO_ACCOUNTS = os.getenv("ENABLE_DEMO_ACCOUNTS", "false").lower() == "true"
DEMO_UID_PLACEHOLDER = "PASTE_UID_HERE"

# -------------------------------------------------------------------------
# STEP REQUIRED ONCE BEFORE FIRST USE:
#   1. Add the 4 phone numbers to Firebase Console (phone numbers for testing)
#   2. Log in once with each number in the app to trigger Firebase user creation
#   3. Run: python demo/scripts/get_demo_uids.py
#   4. Paste the 4 printed UIDs into this dict below
# -------------------------------------------------------------------------
DEMO_FIREBASE_UIDS: dict[str, str] = {
    "+919000000001": DEMO_UID_PLACEHOLDER,
    "+919000000002": DEMO_UID_PLACEHOLDER,
    "+919000000003": DEMO_UID_PLACEHOLDER,
    "+919000000004": DEMO_UID_PLACEHOLDER,
}

DEMO_ACCOUNT_BLUEPRINTS: dict[str, dict[str, object]] = {
    "+919000000001": {
        "checkpoint": "FRESH",
        "persistent": False,
        "description": "Fresh user - no KYC, no wallet, no land",
    },
    "+919000000002": {
        "checkpoint": "KYC_DONE",
        "persistent": False,
        "description": "KYC + wallet done, no land",
    },
    "+919000000003": {
        "checkpoint": "LAND_VERIFIED",
        "persistent": False,
        "description": "KYC + wallet + 1 verified land parcel",
    },
    "+919000000004": {
        "checkpoint": "FULL",
        "persistent": True,
        "description": "Full persistent account with 2024 audit history",
    },
}

# Reverse lookup: firebase_uid -> phone number string.
DEMO_UID_TO_PHONE: dict[str, str] = {
    uid: phone
    for phone, uid in DEMO_FIREBASE_UIDS.items()
    if uid and uid != DEMO_UID_PLACEHOLDER
}

DEMO_ACCOUNT_CONFIG: dict[str, dict[str, object]] = {
    uid: {
        **DEMO_ACCOUNT_BLUEPRINTS[phone],
        "phone": phone,
    }
    for phone, uid in DEMO_FIREBASE_UIDS.items()
    if uid and uid != DEMO_UID_PLACEHOLDER
}


def is_demo_uid(firebase_uid: str) -> bool:
    """Return whether a Firebase UID belongs to a configured demo account."""
    return ENABLE_DEMO_ACCOUNTS and firebase_uid in DEMO_ACCOUNT_CONFIG


def is_resettable_demo(firebase_uid: str) -> bool:
    """Return whether the demo account should reset on the next login."""
    config = DEMO_ACCOUNT_CONFIG.get(firebase_uid, {})
    return ENABLE_DEMO_ACCOUNTS and not bool(config.get("persistent", True))


def get_demo_account(firebase_uid: str) -> dict[str, object] | None:
    """Return the configured demo-account metadata for a Firebase UID."""
    config = DEMO_ACCOUNT_CONFIG.get(firebase_uid)
    return dict(config) if config else None


def get_demo_account_by_phone(phone: str) -> tuple[str | None, dict[str, object] | None]:
    """Return ``(firebase_uid, config)`` for a demo phone number when configured."""
    normalised = phone if phone.startswith("+") else f"+91{phone}"
    firebase_uid = DEMO_FIREBASE_UIDS.get(normalised)
    if not firebase_uid or firebase_uid == DEMO_UID_PLACEHOLDER:
        return None, None
    return firebase_uid, get_demo_account(firebase_uid)


def get_demo_status_accounts() -> list[dict[str, object]]:
    """Return phone-centric demo configuration for setup/status endpoints."""
    accounts: list[dict[str, object]] = []
    for phone, config in DEMO_ACCOUNT_BLUEPRINTS.items():
        firebase_uid = DEMO_FIREBASE_UIDS.get(phone, DEMO_UID_PLACEHOLDER)
        accounts.append(
            {
                "phone": phone,
                "checkpoint": config["checkpoint"],
                "persistent": config["persistent"],
                "description": config["description"],
                "uid_configured": firebase_uid != DEMO_UID_PLACEHOLDER,
            }
        )
    return accounts
