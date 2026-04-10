"""One-time seeding of the TerraTrust demo accounts in the backend database."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.database import _require_async_engine
from demo.checkpoints import (
    CHECKPOINT_BUILDERS,
    DEMO_POLYGON_ACCOUNT4,
    DEMO_WALLET_ADDRESSES,
    _h,
)
from demo.config import DEMO_FIREBASE_UIDS, DEMO_UID_PLACEHOLDER


async def user_exists(firebase_uid: str) -> bool:
    """Return whether a user row already exists for the Firebase UID."""
    engine = _require_async_engine()
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT id FROM users WHERE firebase_uid = :uid"),
            {"uid": firebase_uid},
        )
        return result.first() is not None


async def seed_account_from_checkpoint(phone: str, checkpoint_name: str) -> None:
    """Seed one of the resettable demo accounts from its checkpoint."""
    firebase_uid = DEMO_FIREBASE_UIDS[phone]
    if firebase_uid == DEMO_UID_PLACEHOLDER:
        print(f"  SKIP  {phone} -- UID not configured. Run get_demo_uids.py first.")
        return

    if await user_exists(firebase_uid):
        print(f"  EXISTS {phone} -- skipping (already in database)")
        return

    checkpoint = CHECKPOINT_BUILDERS[checkpoint_name](firebase_uid)
    engine = _require_async_engine()

    async with engine.begin() as conn:
        user_result = await conn.execute(
            text(
                """
                INSERT INTO users (
                    firebase_uid,
                    phone_number,
                    full_name,
                    aadhaar_hash,
                    wallet_address,
                    kyc_completed,
                    role
                ) VALUES (
                    :firebase_uid,
                    :phone_number,
                    :full_name,
                    :aadhaar_hash,
                    :wallet_address,
                    :kyc_completed,
                    :role
                )
                RETURNING id
                """
            ),
            checkpoint["user"],
        )
        user_id = str(user_result.scalar_one())

        for land in checkpoint.get("land_parcels", []):
            await conn.execute(
                text(
                    """
                    INSERT INTO land_parcels (
                        user_id,
                        farm_name,
                        survey_number,
                        district,
                        taluka,
                        village,
                        state,
                        geom,
                        is_verified,
                        boundary_source,
                        ocr_owner_name
                    ) VALUES (
                        :user_id,
                        :farm_name,
                        :survey_number,
                        :district,
                        :taluka,
                        :village,
                        :state,
                        ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(:geom_geojson), 4326)),
                        :is_verified,
                        :boundary_source,
                        :ocr_owner_name
                    )
                    """
                ),
                {**land, "user_id": user_id},
            )

    print(f"  CREATED {phone} [{checkpoint_name}]")


async def seed_account4() -> None:
    """Seed the persistent full-feature demo account."""
    firebase_uid = DEMO_FIREBASE_UIDS["+919000000004"]
    if firebase_uid == DEMO_UID_PLACEHOLDER:
        print("  SKIP  +919000000004 -- UID not configured. Run get_demo_uids.py first.")
        return

    if await user_exists(firebase_uid):
        print("  EXISTS +919000000004 -- skipping (already in database)")
        return

    engine = _require_async_engine()
    async with engine.begin() as conn:
        user_result = await conn.execute(
            text(
                """
                INSERT INTO users (
                    firebase_uid,
                    phone_number,
                    full_name,
                    aadhaar_hash,
                    wallet_address,
                    kyc_completed,
                    role
                ) VALUES (
                    :firebase_uid,
                    :phone_number,
                    :full_name,
                    :aadhaar_hash,
                    :wallet_address,
                    :kyc_completed,
                    :role
                )
                RETURNING id
                """
            ),
            {
                "firebase_uid": firebase_uid,
                "phone_number": "+919000000004",
                "full_name": "Suresh Kumar Demo",
                "aadhaar_hash": _h("999900000004"),
                "wallet_address": DEMO_WALLET_ADDRESSES["+919000000004"],
                "kyc_completed": True,
                "role": "FARMER",
            },
        )
        user_id = str(user_result.scalar_one())

        land_result = await conn.execute(
            text(
                """
                INSERT INTO land_parcels (
                    user_id,
                    farm_name,
                    survey_number,
                    district,
                    taluka,
                    village,
                    state,
                    geom,
                    is_verified,
                    boundary_source,
                    ocr_owner_name
                ) VALUES (
                    :user_id,
                    'Main Demo Farm',
                    'DEMO-004-47',
                    'Pune',
                    'Haveli',
                    'Kharadi',
                    'Maharashtra',
                    ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(:geom_geojson), 4326)),
                    true,
                    'WMS_AUTO',
                    'Suresh Kumar Demo'
                )
                RETURNING id
                """
            ),
            {
                "user_id": user_id,
                "geom_geojson": json.dumps(DEMO_POLYGON_ACCOUNT4),
            },
        )
        land_id = str(land_result.scalar_one())

        await conn.execute(
            text(
                """
                INSERT INTO carbon_audits (
                    land_id,
                    user_id,
                    audit_year,
                    status,
                    s1_vh_mean_db,
                    s1_vv_mean_db,
                    s2_ndvi_mean,
                    s2_evi_mean,
                    gedi_height_mean,
                    srtm_elevation_mean,
                    nisar_used,
                    xgboost_model_version,
                    features_count,
                    trees_scanned_count,
                    total_biomass_tonnes,
                    prev_year_biomass,
                    delta_biomass,
                    carbon_tonnes,
                    co2_equivalent,
                    credits_issued,
                    ipfs_metadata_cid,
                    tx_hash,
                    token_id,
                    minted_at
                ) VALUES (
                    :land_id,
                    :user_id,
                    2024,
                    'MINTED',
                    -13.2,
                    -11.8,
                    0.64,
                    0.58,
                    13.4,
                    541.0,
                    false,
                    'v3.1.0',
                    9,
                    11,
                    12.4,
                    0.0,
                    12.4,
                    5.828,
                    21.38,
                    21.38,
                    'QmDemoAcct4Baseline2024FakeIPFSHashXXXXXXXXXX',
                    :tx_hash,
                    4001,
                    '2024-11-15T10:00:00Z'
                )
                """
            ),
            {
                "land_id": land_id,
                "user_id": user_id,
                "tx_hash": "0x" + ("a" * 64),
            },
        )

    print("  CREATED +919000000004 [FULL persistent -- 2024 audit, 21.38 CTT]")


async def main() -> None:
    """Seed all four demo accounts in the backend database."""
    print("=" * 60)
    print("Seeding demo accounts...")
    print("(Skips any account that already exists)")
    print("=" * 60)

    await seed_account_from_checkpoint("+919000000001", "FRESH")
    await seed_account_from_checkpoint("+919000000002", "KYC_DONE")
    await seed_account_from_checkpoint("+919000000003", "LAND_VERIFIED")
    await seed_account4()

    print()
    print("Done. Login credentials:")
    print("  +91 9000000001  OTP: 111111  resets on next login -> FRESH")
    print("  +91 9000000002  OTP: 222222  resets on next login -> KYC_DONE")
    print("  +91 9000000003  OTP: 333333  resets on next login -> LAND_VERIFIED")
    print("  +91 9000000004  OTP: 444444  NEVER resets (persistent)")


if __name__ == "__main__":
    asyncio.run(main())
