"""Checkpoint restoration logic for resettable demo accounts."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from app.database import _require_async_engine, supabase_client
from demo.checkpoints import CHECKPOINT_BUILDERS
from demo.config import get_demo_account, is_resettable_demo

logger = logging.getLogger("terratrust.demo.restore")


async def restore_to_checkpoint(firebase_uid: str, allow_persistent: bool = False) -> bool:
    """Restore a resettable demo account to its checkpoint state.

    Runs in a single database transaction. Any failure rolls back all changes
    and is logged, but never raised, so ``GET /api/v1/auth/me`` can continue.
    Returns ``True`` only when the checkpoint restore completed successfully.
    """
    if not allow_persistent and not is_resettable_demo(firebase_uid):
        return False

    config = get_demo_account(firebase_uid)
    if not config:
        return False

    builder = CHECKPOINT_BUILDERS.get(str(config["checkpoint"]))
    if builder is None:
        logger.warning("[DEMO] No checkpoint builder found for %s.", config["checkpoint"])
        return False

    checkpoint = builder(firebase_uid)
    engine = _require_async_engine()

    user_id_for_cleanup: str | None = None
    audit_ids_for_cleanup: list[str] = []
    evidence_paths_for_cleanup: set[str] = set()

    try:
        async with engine.begin() as conn:
            user_result = await conn.execute(
                text("SELECT id FROM users WHERE firebase_uid = :uid"),
                {"uid": firebase_uid},
            )
            user_row = user_result.mappings().first()

            if user_row:
                user_id_for_cleanup = str(user_row["id"])
                audit_ids_for_cleanup = await _fetch_audit_ids(conn, user_id_for_cleanup)
                evidence_paths_for_cleanup = await _fetch_evidence_paths(conn, user_id_for_cleanup)

                await conn.execute(
                    text(
                        """
                        DELETE FROM ar_tree_scans
                        WHERE land_id IN (
                            SELECT id FROM land_parcels WHERE user_id = :user_id
                        )
                        """
                    ),
                    {"user_id": user_id_for_cleanup},
                )
                await conn.execute(
                    text(
                        """
                        DELETE FROM sampling_zones
                        WHERE audit_id IN (
                            SELECT id FROM carbon_audits WHERE user_id = :user_id
                        )
                           OR land_id IN (
                            SELECT id FROM land_parcels WHERE user_id = :user_id
                        )
                        """
                    ),
                    {"user_id": user_id_for_cleanup},
                )
                await conn.execute(
                    text("DELETE FROM carbon_audits WHERE user_id = :user_id"),
                    {"user_id": user_id_for_cleanup},
                )
                await conn.execute(
                    text("DELETE FROM wallet_recovery_requests WHERE user_id = :user_id"),
                    {"user_id": user_id_for_cleanup},
                )
                await conn.execute(
                    text("DELETE FROM land_parcels WHERE user_id = :user_id"),
                    {"user_id": user_id_for_cleanup},
                )
                await conn.execute(
                    text("DELETE FROM users WHERE id = :user_id"),
                    {"user_id": user_id_for_cleanup},
                )

            insert_user_result = await conn.execute(
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
            new_user_id = str(insert_user_result.scalar_one())

            land_ids_by_key: dict[str, str] = {}

            for land in checkpoint.get("land_parcels", []):
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
                        RETURNING id
                        """
                    ),
                    {**land, "user_id": new_user_id},
                )
                land_id = str(land_result.scalar_one())
                land_key = str(land.get("key") or land.get("survey_number") or land_id)
                land_ids_by_key[land_key] = land_id

            for audit in checkpoint.get("carbon_audits", []):
                land_key = str(audit.get("land_key") or "")
                land_id = land_ids_by_key.get(land_key)
                if not land_id:
                    logger.warning("[DEMO] Missing land mapping for audit land key %s.", land_key)
                    continue

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
                            :audit_year,
                            :status,
                            :s1_vh_mean_db,
                            :s1_vv_mean_db,
                            :s2_ndvi_mean,
                            :s2_evi_mean,
                            :gedi_height_mean,
                            :srtm_elevation_mean,
                            :nisar_used,
                            :xgboost_model_version,
                            :features_count,
                            :trees_scanned_count,
                            :total_biomass_tonnes,
                            :prev_year_biomass,
                            :delta_biomass,
                            :carbon_tonnes,
                            :co2_equivalent,
                            :credits_issued,
                            :ipfs_metadata_cid,
                            :tx_hash,
                            :token_id,
                            :minted_at
                        )
                        """
                    ),
                    {
                        "land_id": land_id,
                        "user_id": new_user_id,
                        "audit_year": audit.get("audit_year"),
                        "status": audit.get("status"),
                        "s1_vh_mean_db": audit.get("s1_vh_mean_db"),
                        "s1_vv_mean_db": audit.get("s1_vv_mean_db"),
                        "s2_ndvi_mean": audit.get("s2_ndvi_mean"),
                        "s2_evi_mean": audit.get("s2_evi_mean"),
                        "gedi_height_mean": audit.get("gedi_height_mean"),
                        "srtm_elevation_mean": audit.get("srtm_elevation_mean"),
                        "nisar_used": audit.get("nisar_used", False),
                        "xgboost_model_version": audit.get("xgboost_model_version"),
                        "features_count": audit.get("features_count"),
                        "trees_scanned_count": audit.get("trees_scanned_count"),
                        "total_biomass_tonnes": audit.get("total_biomass_tonnes"),
                        "prev_year_biomass": audit.get("prev_year_biomass"),
                        "delta_biomass": audit.get("delta_biomass"),
                        "carbon_tonnes": audit.get("carbon_tonnes"),
                        "co2_equivalent": audit.get("co2_equivalent"),
                        "credits_issued": audit.get("credits_issued"),
                        "ipfs_metadata_cid": audit.get("ipfs_metadata_cid"),
                        "tx_hash": audit.get("tx_hash"),
                        "token_id": audit.get("token_id"),
                        "minted_at": audit.get("minted_at"),
                    },
                )

        _cleanup_storage(user_id_for_cleanup, audit_ids_for_cleanup, evidence_paths_for_cleanup)
        logger.info(
            "[DEMO] Restored %s to '%s'.",
            config["phone"],
            config["checkpoint"],
        )
        return True
    except Exception as exc:
        logger.exception("[DEMO] Restore error for %s: %s", firebase_uid, exc)
        return False


async def _fetch_audit_ids(conn: Any, user_id: str) -> list[str]:
    """Return audit IDs associated with a demo user before reset."""
    result = await conn.execute(
        text("SELECT id FROM carbon_audits WHERE user_id = :user_id"),
        {"user_id": user_id},
    )
    return [str(row["id"]) for row in result.mappings().all()]


async def _fetch_evidence_paths(conn: Any, user_id: str) -> set[str]:
    """Return evidence-photo object paths linked to a demo user before reset."""
    result = await conn.execute(
        text(
            """
            SELECT evidence_photo_path
            FROM ar_tree_scans
            WHERE land_id IN (
                SELECT id FROM land_parcels WHERE user_id = :user_id
            )
              AND evidence_photo_path IS NOT NULL
            """
        ),
        {"user_id": user_id},
    )
    return {str(row["evidence_photo_path"]) for row in result.mappings().all()}


def _cleanup_storage(
    user_id: str | None,
    audit_ids: list[str],
    evidence_paths: set[str],
) -> None:
    """Delete Supabase Storage files associated with the old demo-account state."""
    try:
        land_document_paths: set[str] = set()
        if user_id:
            land_document_paths.update(_collect_storage_paths("land-documents", [user_id]))

        evidence_photo_paths = set(evidence_paths)
        if audit_ids:
            evidence_photo_paths.update(_collect_storage_paths("evidence-photos", audit_ids))

        _remove_bucket_paths("land-documents", land_document_paths)
        _remove_bucket_paths("evidence-photos", evidence_photo_paths)
    except Exception as exc:
        logger.warning("[DEMO] Storage cleanup warning: %s", exc)


def _collect_storage_paths(bucket: str, prefixes: list[str]) -> set[str]:
    """Collect object paths under the given storage prefixes."""
    collected: set[str] = set()
    for prefix in prefixes:
        _collect_storage_paths_for_prefix(bucket, prefix.strip("/"), collected)
    return collected


def _collect_storage_paths_for_prefix(bucket: str, prefix: str, collected: set[str]) -> None:
    """Recursively collect file paths beneath a storage prefix."""
    try:
        entries = supabase_client.storage.from_(bucket).list(path=prefix)
    except Exception as exc:
        logger.warning("[DEMO] Could not list storage bucket %s at %s: %s", bucket, prefix, exc)
        return

    for entry in entries or []:
        name = str(entry.get("name") or "").strip("/")
        if not name:
            continue

        object_path = f"{prefix}/{name}" if prefix else name
        if entry.get("id") or entry.get("metadata"):
            collected.add(object_path)
        else:
            _collect_storage_paths_for_prefix(bucket, object_path, collected)


def _remove_bucket_paths(bucket: str, paths: set[str]) -> None:
    """Remove storage object paths in small chunks so cleanup stays reliable."""
    if not paths:
        return

    bucket_api = supabase_client.storage.from_(bucket)
    sorted_paths = sorted(paths)
    for index in range(0, len(sorted_paths), 100):
        chunk = sorted_paths[index : index + 100]
        try:
            bucket_api.remove(chunk)
        except Exception as exc:
            logger.warning("[DEMO] Failed to remove %d path(s) from %s: %s", len(chunk), bucket, exc)
