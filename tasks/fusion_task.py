"""Background fusion task for audit calculation and minting handoff."""

import asyncio
import logging
from datetime import datetime, timezone

from tasks.celery_app import celery_app
from app.database import (
    fetch_land_parcel_record,
    list_sampling_zones_for_audit,
    list_tree_scans_for_audit,
    supabase_client,
    update_tree_scan_measurements,
)
from services import fusion_engine

logger = logging.getLogger("terratrust.tasks.fusion")
FUSION_RETRY_DELAYS_SECONDS = (5 * 60, 30 * 60)


def _fusion_retry_delay_seconds(retry_count: int) -> int:
    """Return the SRS-defined delay for the next fusion retry."""
    index = min(max(retry_count, 0), len(FUSION_RETRY_DELAYS_SECONDS) - 1)
    return FUSION_RETRY_DELAYS_SECONDS[index]


@celery_app.task(bind=True, max_retries=2)
def run_audit_fusion(self, audit_id: str) -> dict:
    """Execute the full fusion pipeline for a single audit.

    Workflow
    --------
    1. Mark audit status → ``CALCULATING``.
    2. Fetch audit + land data from Supabase.
    3. Run ``fusion_engine.run_fusion()``.
    4. Run ``fusion_engine.calculate_credits()``.
    5. Persist results in ``carbon_audits``.
    6. Trigger ``minting_task`` to mint the audit certificate and any credits.

    On exception the task retries up to 2 times with the documented
    5-minute then 30-minute backoff, then marks the audit ``FAILED``.
    """
    try:
        # --- 1. Update status -----------------------------------------------
        supabase_client.table("carbon_audits").update(
            {"status": "CALCULATING"}
        ).eq("id", audit_id).execute()

        # --- 2. Fetch audit data --------------------------------------------
        audit_resp = (
            supabase_client.table("carbon_audits")
            .select("*")
            .eq("id", audit_id)
            .single()
            .execute()
        )
        audit_data = audit_resp.data

        land_id = audit_data["land_id"]
        audit_year = audit_data.get("audit_year", datetime.now(timezone.utc).year)

        # Fetch land boundary
        land_data = asyncio.run(fetch_land_parcel_record(land_id))
        boundary_geojson = land_data.get("boundary_geojson")
        if not boundary_geojson:
            raise ValueError(f"Land parcel {land_id} is missing boundary geometry.")

        # Fetch tree scans
        tree_scans = asyncio.run(list_tree_scans_for_audit(audit_id))
        sampling_zones = asyncio.run(list_sampling_zones_for_audit(audit_id))

        # --- 3. Run fusion --------------------------------------------------
        fusion_result = fusion_engine.run_fusion(
            audit_id=audit_id,
            land_id=land_id,
            tree_scans=tree_scans,
            land_boundary_geojson=boundary_geojson,
            audit_year=audit_year,
            sampling_zones=sampling_zones,
        )

        # --- 4. Calculate credits -------------------------------------------
        credit_result = fusion_engine.calculate_credits(
            total_biomass_tonnes=fusion_result["total_biomass_tonnes"],
            land_id=land_id,
            audit_year=audit_year,
        )

        asyncio.run(
            update_tree_scan_measurements(fusion_result.get("tree_measurements", []))
        )

        # --- 5. Persist results ---------------------------------------------
        satellite_features = fusion_result.get("satellite_features", {})
        update_payload = {
            "total_biomass_tonnes": fusion_result["total_biomass_tonnes"],
            "credits_issued": credit_result["credits_issued"],
            "prev_year_biomass": credit_result.get("prev_year_biomass"),
            "delta_biomass": credit_result["delta_biomass"],
            "carbon_tonnes": credit_result["carbon_tonnes"],
            "co2_equivalent": credit_result["co2_equivalent"],
            "satellite_features": satellite_features,
            "s1_vh_mean_db": satellite_features.get("s1_vh_mean_db"),
            "s1_vv_mean_db": satellite_features.get("s1_vv_mean_db"),
            "s2_ndvi_mean": satellite_features.get("s2_ndvi_mean"),
            "s2_evi_mean": satellite_features.get("s2_evi_mean"),
            "gedi_height_mean": satellite_features.get("gedi_height_mean"),
            "srtm_elevation_mean": satellite_features.get("srtm_elevation_mean"),
            "srtm_slope_mean": satellite_features.get("srtm_slope_mean"),
            "nisar_used": satellite_features.get("nisar_used", False),
            "features_count": satellite_features.get("features_count"),
            "trees_scanned_count": len(tree_scans),
            "xgboost_model_version": satellite_features.get("processing_method", "S1_S2_GEDI_SRTM_XGBoost_v3.1"),
            "reason": credit_result.get("reason"),
            "calculated_at": datetime.now(timezone.utc).isoformat(),
        }

        if credit_result["credits_issued"] > 0:
            update_payload["status"] = "READY_TO_MINT"
            supabase_client.table("carbon_audits").update(
                update_payload
            ).eq("id", audit_id).execute()

            from tasks.minting_task import run_minting

            run_minting.delay(
                audit_id=audit_id,
                land_id=land_id,
                audit_year=audit_year,
            )
            logger.info(
                "Fusion done for audit %s — %.2f credits, minting enqueued.",
                audit_id,
                credit_result["credits_issued"],
            )
        else:
            update_payload["status"] = "COMPLETE_NO_CREDITS"
            supabase_client.table("carbon_audits").update(
                update_payload
            ).eq("id", audit_id).execute()
            logger.info(
                "Fusion done for audit %s — no credits issued (%s).",
                audit_id,
                credit_result.get("reason", "no eligible biomass growth"),
            )

        return {
            "audit_id": audit_id,
            "credits_issued": credit_result["credits_issued"],
        }

    except Exception as exc:
        logger.error("Fusion task failed for audit %s: %s", audit_id, exc)
        # Mark as FAILED on final retry
        if self.request.retries >= self.max_retries:
            supabase_client.table("carbon_audits").update(
                {"status": "FAILED", "error": str(exc)[:500]}
            ).eq("id", audit_id).execute()
            raise

        countdown = _fusion_retry_delay_seconds(self.request.retries)
        next_attempt = self.request.retries + 2
        total_attempts = self.max_retries + 1
        logger.warning(
            "Scheduling fusion retry for audit %s in %d seconds (attempt %d/%d).",
            audit_id,
            countdown,
            next_attempt,
            total_attempts,
        )
        raise self.retry(exc=exc, countdown=countdown)
