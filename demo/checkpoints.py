"""Checkpoint builders for development-only demo accounts."""

from __future__ import annotations

import hashlib
import json

from demo.config import DEMO_UID_TO_PHONE

# Pre-seeded fake polygon - small area near Pune, Maharashtra (~0.49 ha)
DEMO_POLYGON_ACCOUNT3 = {
    "type": "Polygon",
    "coordinates": [
        [
            [73.856, 18.520],
            [73.858, 18.520],
            [73.858, 18.522],
            [73.856, 18.522],
            [73.856, 18.520],
        ]
    ],
}

# Different polygon for account 4 so survey numbers never conflict.
DEMO_POLYGON_ACCOUNT4 = {
    "type": "Polygon",
    "coordinates": [
        [
            [73.859, 18.523],
            [73.861, 18.523],
            [73.861, 18.525],
            [73.859, 18.525],
            [73.859, 18.523],
        ]
    ],
}

DEMO_WALLET_ADDRESSES: dict[str, str | None] = {
    "+919000000001": None,
    "+919000000002": "0x0000000000000000000000000000000000002002",
    "+919000000003": "0x0000000000000000000000000000000000003003",
    "+919000000004": "0x0000000000000000000000000000000000004004",
}


def _h(value: str) -> str:
    """SHA-256 hash matching the backend KYC hashing used by ``POST /auth/kyc``."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _phone_for_uid(firebase_uid: str) -> str:
    """Return the configured demo phone number for a Firebase UID."""
    phone = DEMO_UID_TO_PHONE.get(firebase_uid)
    if not phone:
        raise KeyError(f"Demo phone mapping not found for Firebase UID '{firebase_uid}'.")
    return phone


def checkpoint_fresh(firebase_uid: str) -> dict[str, object]:
    """Return the FRESH checkpoint for account 1."""
    phone = _phone_for_uid(firebase_uid)
    return {
        "user": {
            "firebase_uid": firebase_uid,
            "phone_number": phone,
            "full_name": None,
            "aadhaar_hash": None,
            "wallet_address": DEMO_WALLET_ADDRESSES[phone],
            "kyc_completed": False,
            "role": "FARMER",
        },
        "land_parcels": [],
    }


def checkpoint_kyc_done(firebase_uid: str) -> dict[str, object]:
    """Return the KYC_DONE checkpoint for account 2."""
    phone = _phone_for_uid(firebase_uid)
    return {
        "user": {
            "firebase_uid": firebase_uid,
            "phone_number": phone,
            "full_name": "Demo Farmer Two",
            "aadhaar_hash": _h("999900000002"),
            "wallet_address": DEMO_WALLET_ADDRESSES[phone],
            "kyc_completed": True,
            "role": "FARMER",
        },
        "land_parcels": [],
    }


def checkpoint_land_verified(firebase_uid: str) -> dict[str, object]:
    """Return the LAND_VERIFIED checkpoint for account 3."""
    phone = _phone_for_uid(firebase_uid)
    return {
        "user": {
            "firebase_uid": firebase_uid,
            "phone_number": phone,
            "full_name": "Ramesh Shankar Patil",
            "aadhaar_hash": _h("999900000003"),
            "wallet_address": DEMO_WALLET_ADDRESSES[phone],
            "kyc_completed": True,
            "role": "FARMER",
        },
        "land_parcels": [
            {
                "farm_name": "Demo North Field",
                "survey_number": "DEMO-003-47",
                "district": "Pune",
                "taluka": "Haveli",
                "village": "Kharadi",
                "state": "Maharashtra",
                "geom_geojson": json.dumps(DEMO_POLYGON_ACCOUNT3),
                "is_verified": True,
                "boundary_source": "WMS_AUTO",
                "ocr_owner_name": "Ramesh Shankar Patil",
            }
        ],
    }


CHECKPOINT_BUILDERS: dict[str, object] = {
    "FRESH": checkpoint_fresh,
    "KYC_DONE": checkpoint_kyc_done,
    "LAND_VERIFIED": checkpoint_land_verified,
}
