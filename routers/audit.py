"""Audit router aligned to the documented audit and history API contracts."""

import base64
import hashlib
import io
import logging
from collections import Counter
from datetime import datetime, timezone
import math
from typing import Any, Dict, List
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from PIL import Image, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool

from app.database import (
    delete_tree_scan_records_for_audit,
    fetch_land_parcel_record,
    insert_sampling_zone_records,
    insert_tree_scan_record,
    land_contains_point,
    list_sampling_zones_for_audit,
    list_tree_scans_for_audit,
    supabase_client,
)
from app.dependencies import get_current_user
from app.rate_limit import RateLimitSpec, enforce_rate_limit
from models.audit import (
    AuditHistoryItem,
    AuditHistoryResponse,
    AuditResultResponse,
    AuditSubmitRequest,
    AuditSubmitResponse,
    AuditZonesResponse,
    ZoneResponse,
)
from services import zone_generation_service
from services.fusion_engine import normalise_species_name, wood_density_for_species
from services.ipfs_service import to_gateway_url

try:
    from demo.config import is_demo_uid
    from demo.middleware import invalidate_demo_session
except Exception:  # pragma: no cover - demo tooling is optional outside demo-enabled environments.
    def is_demo_uid(_firebase_uid: str) -> bool:
        return False

    def invalidate_demo_session(_firebase_uid: str) -> None:
        return None

logger = logging.getLogger("terratrust.audit")

router = APIRouter()

MAX_EVIDENCE_WIDTH = 1280
MAX_EVIDENCE_HEIGHT = 960
AUDIT_ZONES_RATE_LIMIT = RateLimitSpec(
    scope="audit.zones",
    limit=5,
    window_seconds=60 * 60,
    error_message="Too many audit zone requests. Please try again later.",
)
AUDIT_SUBMIT_RATE_LIMIT = RateLimitSpec(
    scope="audit.submit-samples",
    limit=3,
    window_seconds=60 * 60,
    error_message="Too many audit submission requests. Please try again later.",
)
AUDIT_RESULT_RATE_LIMIT = RateLimitSpec(
    scope="audit.result",
    limit=60,
    window_seconds=60,
    error_message="Too many audit status requests. Please wait before polling again.",
)


def _processing_submit_response(audit_id: str) -> AuditSubmitResponse:
    """Return the documented acknowledgement payload for non-terminal audit submissions."""
    return AuditSubmitResponse(
        status="PROCESSING",
        audit_id=audit_id,
        estimated_seconds=60,
        message="Satellite verification in progress",
    )


def _zone_id_from_record(zone: Dict[str, Any]) -> str:
    """Return the canonical zone identifier for generated or persisted zone rows."""
    return str(zone.get("zone_id") or zone.get("id") or "")


def _zone_label_from_record(zone: Dict[str, Any]) -> str:
    """Return the human-facing zone label for generated or persisted zone rows."""
    return str(zone.get("label") or zone.get("zone_label") or "")


def _build_zone_responses(zones: List[Dict[str, Any]]) -> List[ZoneResponse]:
    """Convert generated or persisted zone rows into API response models."""
    return [
        ZoneResponse(
            zone_id=_zone_id_from_record(zone),
            label=_zone_label_from_record(zone),
            centre_gps=zone["centre_gps"],
            radius_metres=zone["radius_metres"],
            zone_type=zone["zone_type"],
            sequence_order=zone["sequence_order"],
            gedi_available=zone["gedi_available"],
        )
        for zone in zones
    ]


def _walking_path_metres_for_zones(zones: List[Dict[str, Any]]) -> float:
    """Recompute walking-path distance from the stored zone ordering."""
    if len(zones) < 2:
        return 0.0

    ordered_zones = sorted(
        zones,
        key=lambda zone: (zone.get("sequence_order") or 0, _zone_id_from_record(zone)),
    )

    total_walk = 0.0
    for index in range(len(ordered_zones) - 1):
        first_centre = ordered_zones[index]["centre_gps"]
        second_centre = ordered_zones[index + 1]["centre_gps"]
        total_walk += _distance_metres(
            float(first_centre["lat"]),
            float(first_centre["lng"]),
            float(second_centre["lat"]),
            float(second_centre["lng"]),
        )

    return round(total_walk, 1)


def _build_audit_zones_response(audit_id: str, zones: List[Dict[str, Any]]) -> AuditZonesResponse:
    """Build a stable zones payload for new or resumed audits."""
    zone_responses = _build_zone_responses(zones)
    return AuditZonesResponse(
        audit_id=audit_id,
        zones=zone_responses,
        walking_path_metres=_walking_path_metres_for_zones(zones),
        min_trees_required=len(zone_responses) * 3,
    )


def _count_completed_zones(
    zones: List[Dict[str, Any]],
    tree_scans: List[Dict[str, Any]],
) -> int:
    """Count how many zones already have the documented minimum three samples."""
    if not zones or not tree_scans:
        return 0

    scan_counts = Counter(
        str(scan.get("zone_id"))
        for scan in tree_scans
        if scan.get("zone_id")
    )
    return sum(1 for zone in zones if scan_counts.get(_zone_id_from_record(zone), 0) >= 3)


def _build_processing_result_payload(
    current_status: str,
    audit: Dict[str, Any],
    zones: List[Dict[str, Any]],
    tree_scans: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Expose the real non-terminal audit phase so mobile clients can resume cleanly."""
    zones_total = len(zones)
    min_trees_required = zones_total * 3 if zones_total else None
    trees_scanned_count = audit.get("trees_scanned_count")
    trees_submitted = int(trees_scanned_count) if trees_scanned_count is not None else len(tree_scans)
    zones_completed = _count_completed_zones(zones, tree_scans)

    payload: Dict[str, Any] = {
        "status": current_status,
        "trees_submitted": trees_submitted,
        "min_trees_required": min_trees_required,
        "zones_total": zones_total,
        "zones_completed": zones_completed,
        "can_resume_scanning": current_status == "PROCESSING",
    }

    if current_status == "PROCESSING":
        if audit.get("error"):
            payload.update(
                {
                    "phase": "RETRY_SUBMISSION",
                    "message": audit["error"],
                }
            )
            return payload

        if zones_total and trees_submitted == 0:
            payload.update(
                {
                    "phase": "AWAITING_SAMPLES",
                    "message": (
                        "Sampling zones are ready. Resume scanning and submit at least "
                        f"{min_trees_required} trees to continue this audit."
                    ),
                }
            )
            return payload

        payload.update(
            {
                "phase": "AWAITING_SAMPLES",
                "message": "Resume scanning and resubmit the required tree samples to continue this audit.",
            }
        )
        return payload

    if current_status == "CALCULATING":
        payload.update(
            {
                "phase": "CALCULATING",
                "message": "Tree samples submitted. Satellite verification is running.",
                "can_resume_scanning": False,
            }
        )
        return payload

    payload.update(
        {
            "phase": "READY_TO_MINT",
            "message": "Audit calculation is complete. Final credit minting is pending.",
            "can_resume_scanning": False,
        }
    )
    return payload


def _maybe_invalidate_demo_checkpoint(current_user: Dict[str, Any]) -> None:
    """Allow demo accounts to reset back to their baseline after an in-progress audit."""
    firebase_uid = str(current_user.get("firebase_uid") or "")
    if firebase_uid and is_demo_uid(firebase_uid):
        invalidate_demo_session(firebase_uid)

def _normalise_species_name(species: str) -> str:
    """Return the canonical supported species name or fail clearly."""
    try:
        return normalise_species_name(species)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


def _decode_evidence_photo(photo_base64: str, expected_hash: str) -> bytes:
    """Decode and verify an evidence photo payload."""
    try:
        photo_bytes = base64.b64decode(photo_base64, validate=True)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Evidence photo must be valid base64-encoded image data.",
        ) from exc

    if not photo_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Evidence photo is empty.",
        )

    computed_hash = hashlib.sha256(photo_bytes).hexdigest()
    if computed_hash.lower() != expected_hash.strip().lower():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Evidence photo hash does not match the uploaded photo bytes.",
        )

    try:
        with Image.open(io.BytesIO(photo_bytes)) as image:
            image.load()

            if image.format != "JPEG":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Evidence photo must be final JPEG bytes without a data URI prefix.",
                )

            if image.width > MAX_EVIDENCE_WIDTH or image.height > MAX_EVIDENCE_HEIGHT:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "Evidence photo exceeds the maximum supported size of "
                        f"{MAX_EVIDENCE_WIDTH}x{MAX_EVIDENCE_HEIGHT}."
                    ),
                )

            exif = image.getexif()
            if exif and len(exif) > 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Evidence photo must be EXIF-stripped before upload.",
                )
    except HTTPException:
        raise
    except UnidentifiedImageError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Evidence photo bytes do not decode into a valid JPEG image.",
        ) from exc

    return photo_bytes


def _distance_metres(first_lat: float, first_lng: float, second_lat: float, second_lng: float) -> float:
    """Approximate straight-line distance between two GPS points."""
    dlat = (second_lat - first_lat) * 111_320
    dlng = (second_lng - first_lng) * 111_320 * math.cos(
        math.radians((first_lat + second_lat) / 2)
    )
    return math.sqrt(dlat**2 + dlng**2)


def _to_utc_iso(timestamp: datetime) -> str:
    """Normalize a timestamp to UTC ISO-8601 for persistence."""
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc).isoformat()


def _validate_species_submission(tree) -> None:
    """Validate species confidence against the documented species_source contract."""
    if tree.species_source == "MODEL_AUTO":
        if tree.species_confidence < 0.80:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Tree sample in zone '{tree.zone_id}' must use MODEL_AUTO only when "
                    "species_confidence is at least 0.80."
                ),
            )
        return

    if tree.species_source == "MODEL_CONFIRMED":
        if not (0.60 <= tree.species_confidence < 0.80):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Tree sample in zone '{tree.zone_id}' must use MODEL_CONFIRMED only when "
                    "species_confidence is between 0.60 and 0.79."
                ),
            )
        return

    if tree.species_source == "MANUAL_SELECTED" and tree.species_confidence >= 0.60:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Tree sample in zone '{tree.zone_id}' must use MANUAL_SELECTED only after a "
                "low-confidence or NOT_APPROVED model result."
            ),
        )


# ---------------------------------------------------------------------------
# GET /zones
# ---------------------------------------------------------------------------
@router.get("/zones", response_model=AuditZonesResponse)
async def get_audit_zones(
    land_id: str = Query(..., description="UUID of the registered land parcel"),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Generate NDVI-stratified sampling zones for a land parcel.

    Creates a new audit record in Supabase with status ``PROCESSING``
    and returns the list of zones with walking path estimate.
    """
    enforce_rate_limit(current_user["id"], AUDIT_ZONES_RATE_LIMIT)

    try:
        land_data = await fetch_land_parcel_record(land_id)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Land parcel '{land_id}' not found.",
        ) from exc
    except Exception as exc:
        logger.error("Failed to load land parcel %s for zone generation: %s", land_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load land parcel data.",
        ) from exc

    # Ownership check
    if land_data.get("user_id") != current_user["id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not own this land parcel.",
        )

    boundary_geojson = land_data.get("boundary_geojson")
    if not boundary_geojson:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Registered land parcel is missing boundary geometry.",
        )

    audit_year = datetime.now(timezone.utc).year
    existing_audit_resp = await run_in_threadpool(
        lambda: (
            supabase_client.table("carbon_audits")
            .select("id, status")
            .eq("land_id", land_id)
            .eq("audit_year", audit_year)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
    )
    existing_audit = (existing_audit_resp.data or [None])[0]
    audit_id = str(uuid.uuid4())

    if existing_audit:
        existing_status = existing_audit.get("status")
        if existing_status in {"MINTED", "COMPLETE_NO_CREDITS"}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A carbon audit for {audit_year} has already been completed for this land.",
            )

        if existing_status == "PROCESSING":
            existing_zones = await list_sampling_zones_for_audit(existing_audit["id"])
            if existing_zones:
                _maybe_invalidate_demo_checkpoint(current_user)
                logger.info(
                    "Resuming existing audit %s for land %s with %d persisted zones.",
                    existing_audit["id"],
                    land_id,
                    len(existing_zones),
                )
                return _build_audit_zones_response(existing_audit["id"], existing_zones)

        if existing_status in {"CALCULATING", "READY_TO_MINT"}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "An audit already exists for this land in the current audit year.",
                    "existing_audit_id": existing_audit["id"],
                    "status": existing_status,
                },
            )

        audit_id = existing_audit["id"]
        await delete_tree_scan_records_for_audit(audit_id)
        await run_in_threadpool(
            lambda: supabase_client.table("sampling_zones").delete().eq("audit_id", audit_id).execute()
        )
        await run_in_threadpool(
            lambda: (
                supabase_client.table("carbon_audits")
                .update(
                    {
                        "status": "PROCESSING",
                        "error": None,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                .eq("id", audit_id)
                .execute()
            )
        )

    # Generate zones
    try:
        zones = await run_in_threadpool(
            zone_generation_service.generate_sampling_zones,
            land_id,
            boundary_geojson,
        )
    except Exception as exc:
        logger.error("Zone generation failed for land %s: %s", land_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate sampling zones. Ensure GEE is configured.",
        ) from exc

    # Create or refresh the audit record
    if not existing_audit:
        await run_in_threadpool(
            lambda: (
                supabase_client.table("carbon_audits")
                .insert(
                    {
                        "id": audit_id,
                        "land_id": land_id,
                        "user_id": current_user["id"],
                        "audit_year": audit_year,
                        "status": "PROCESSING",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                .execute()
            )
        )

    await insert_sampling_zone_records(land_id=land_id, audit_id=audit_id, zones=zones)
    _maybe_invalidate_demo_checkpoint(current_user)
    return _build_audit_zones_response(audit_id, zones)


# ---------------------------------------------------------------------------
# POST /submit-samples
# ---------------------------------------------------------------------------
@router.post(
    "/submit-samples",
    response_model=AuditSubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_samples(
    body: AuditSubmitRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Accept tree-scan samples and kick off the fusion pipeline.

    Validations
    -----------
    - Minimum **9** trees total.
    - At least **3** trees per zone.
    - Each tree must have valid photo data.
    """
    enforce_rate_limit(current_user["id"], AUDIT_SUBMIT_RATE_LIMIT)

    trees = body.trees

    try:
        audit_resp = await run_in_threadpool(
            lambda: (
                supabase_client.table("carbon_audits")
                .select("id, user_id, land_id, status")
                .eq("id", body.audit_id)
                .maybe_single()
                .execute()
            )
        )
        audit_data = audit_resp.data
    except Exception as exc:
        logger.error("Failed to load audit %s for sample submission: %s", body.audit_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load audit session.",
        ) from exc

    if not audit_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit '{body.audit_id}' not found.",
        )

    if audit_data.get("user_id") != current_user["id"] or audit_data.get("land_id") != body.land_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this audit session.",
        )

    current_status = audit_data.get("status")
    if current_status in {"CALCULATING", "READY_TO_MINT"}:
        return _processing_submit_response(body.audit_id)

    if current_status in {"MINTED", "COMPLETE_NO_CREDITS", "FAILED"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This audit is already in a terminal state and cannot be resubmitted.",
        )

    if current_status != "PROCESSING":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tree samples can only be submitted while the audit is in PROCESSING state.",
        )

    # --- Ensure submitted zones belong to the audit -----------------------
    try:
        sampling_zones = await list_sampling_zones_for_audit(body.audit_id)
        zone_map = {zone["id"]: zone for zone in sampling_zones}
        valid_zone_ids = set(zone_map)
        if not valid_zone_ids:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No sampling zones were found for this audit session.",
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to validate sampling zones for this audit.",
        ) from exc

    min_trees_required = len(valid_zone_ids) * 3
    if len(trees) < min_trees_required:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Minimum {min_trees_required} trees required for this audit.",
        )

    # --- Species and per-zone checks ---------------------------------------
    zone_counts: Dict[str, int] = Counter(t.zone_id for t in trees)
    canonical_species: Dict[int, str] = {}
    for index, tree in enumerate(trees):
        _validate_species_submission(tree)
        canonical_species[index] = _normalise_species_name(tree.species)

    invalid_zone_ids = sorted(
        {tree.zone_id for tree in trees if tree.zone_id not in valid_zone_ids}
    )
    if invalid_zone_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Submitted tree samples contain zone IDs that do not belong "
                f"to audit '{body.audit_id}': {', '.join(invalid_zone_ids)}."
            ),
        )

    for zone_id, count in zone_counts.items():
        if count < 3:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Zone '{zone_id}' has only {count} tree(s). "
                    "Minimum 3 per zone."
                ),
            )
        if count > 5:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Zone '{zone_id}' has {count} tree samples. "
                    "Maximum 5 per zone."
                ),
            )

    # --- GPS accuracy check (≤ 30 m) --------------------------------------
    for tree in trees:
        if tree.gps_accuracy_m > 30:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Tree sample in zone '{tree.zone_id}' has GPS accuracy "
                    f"{tree.gps_accuracy_m:.1f} m. Maximum allowed is 30 m."
                ),
            )

    # --- Zone geometry + GEDI checks --------------------------------------
    try:
        missing_zone_ids = sorted(valid_zone_ids - set(zone_counts))
        if missing_zone_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "At least 3 trees must be submitted for every sampling zone. Missing zone IDs: "
                    + ", ".join(missing_zone_ids)
                    + "."
                ),
            )

        for tree in trees:
            zone = zone_map[tree.zone_id]
            centre = zone["centre_gps"]
            radius_metres = float(zone["radius_metres"])

            if not zone.get("gedi_available") and tree.height_m is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Tree sample in zone '{tree.zone_id}' requires AR fallback height "
                        "because GEDI is unavailable for this zone."
                    ),
                )

            distance_metres = _distance_metres(
                tree.gps.lat,
                tree.gps.lng,
                float(centre["lat"]),
                float(centre["lng"]),
            )
            if distance_metres > radius_metres:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Tree sample for zone '{tree.zone_id}' lies {distance_metres:.1f} m from "
                        f"the zone centre. Maximum allowed is {radius_metres:.1f} m."
                    ),
                )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to validate sampling zones for this audit.",
        ) from exc

    # --- Validate each tree before mutating persisted scans ----------------
    prepared_scans: List[Dict[str, Any]] = []
    for index, tree in enumerate(trees):
        species_name = canonical_species[index]
        wood_density = wood_density_for_species(species_name)

        if not await land_contains_point(body.land_id, tree.gps.lat, tree.gps.lng):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Tree sample GPS point lies outside the registered land boundary.",
            )

        photo_bytes = _decode_evidence_photo(
            tree.evidence_photo_base64,
            tree.evidence_photo_hash,
        )

        prepared_scans.append(
            {
                "tree": tree,
                "species_name": species_name,
                "wood_density": wood_density,
                "photo_bytes": photo_bytes,
            }
        )

    # --- Process each tree ------------------------------------------------
    await delete_tree_scan_records_for_audit(body.audit_id)

    for prepared_scan in prepared_scans:
        tree = prepared_scan["tree"]
        species_name = prepared_scan["species_name"]
        wood_density = prepared_scan["wood_density"]
        photo_bytes = prepared_scan["photo_bytes"]

        scan_id = str(uuid.uuid4())
        photo_storage_path = f"{body.audit_id}/{scan_id}.jpg"
        try:
            await run_in_threadpool(
                lambda: supabase_client.storage.from_("evidence-photos").upload(
                    photo_storage_path,
                    photo_bytes,
                )
            )
        except Exception as exc:
            logger.error("Photo upload failed for audit %s: %s", body.audit_id, exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to store tree evidence photo.",
            ) from exc

        # Insert into ar_tree_scans table
        scan_record = {
            "id": scan_id,
            "audit_id": body.audit_id,
            "land_id": body.land_id,
            "zone_id": tree.zone_id,
            "species": species_name,
            "species_confidence": tree.species_confidence,
            "species_source": tree.species_source,
            "dbh_cm": tree.dbh_cm,
            "height_m": tree.height_m,
            "gps": {"lat": tree.gps.lat, "lng": tree.gps.lng},
            "gps_accuracy_m": tree.gps_accuracy_m,
            "gedi_height_m": None,
            "height_source": "AR_FALLBACK" if tree.height_m else None,
            "ar_tier_used": tree.ar_tier_used,
            "confidence_score": tree.confidence_score,
            "evidence_photo_hash": tree.evidence_photo_hash,
            "evidence_photo_path": photo_storage_path,
            "wood_density": wood_density,
            "agb_kg": None,
            "scan_timestamp": _to_utc_iso(tree.scan_timestamp),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await insert_tree_scan_record(scan_record)

    # --- Update audit status -----------------------------------------------
    await run_in_threadpool(
        lambda: (
            supabase_client.table("carbon_audits")
            .update({"status": "CALCULATING", "error": None})
            .eq("id", body.audit_id)
            .execute()
        )
    )

    # --- Trigger fusion task -----------------------------------------------
    from tasks.fusion_task import run_audit_fusion

    try:
        run_audit_fusion.delay(body.audit_id)
    except Exception as exc:
        logger.error("Failed to enqueue fusion task for audit %s: %s", body.audit_id, exc)
        await run_in_threadpool(
            lambda: (
                supabase_client.table("carbon_audits")
                .update(
                    {
                        "status": "PROCESSING",
                        "error": "Fusion task queue is temporarily unavailable.",
                    }
                )
                .eq("id", body.audit_id)
                .execute()
            )
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Satellite processing queue is temporarily unavailable. Please try submitting again.",
        ) from exc

    logger.info(
        "Submitted %d tree samples for audit %s — fusion enqueued.",
        len(trees),
        body.audit_id,
    )

    return _processing_submit_response(body.audit_id)


# ---------------------------------------------------------------------------
# GET /result/{audit_id}
# ---------------------------------------------------------------------------
@router.get(
    "/result/{audit_id}",
    response_model=AuditResultResponse,
    response_model_exclude_none=True,
)
async def get_audit_result(
    audit_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Retrieve the current status or final result of an audit.

    Returns
    -------
    - ``status='MINTED'`` → full results including tx_hash and IPFS URL.
    - ``status='CALCULATING'`` or ``'PROCESSING'`` → still in progress.
    - ``status='FAILED'`` → error description.
    """
    enforce_rate_limit(current_user["id"], AUDIT_RESULT_RATE_LIMIT)

    try:
        resp = (
            supabase_client.table("carbon_audits")
            .select("*")
            .eq("id", audit_id)
            .maybe_single()
            .execute()
        )
        audit = resp.data
    except Exception as exc:
        logger.error("Failed to load audit result for %s: %s", audit_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load audit result.",
        ) from exc

    if not audit:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit '{audit_id}' not found.",
        )

    if audit.get("user_id") != current_user["id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this audit.",
        )

    current_status = audit.get("status", "UNKNOWN")

    if current_status == "MINTED":
        return {
            "status": "MINTED",
            "total_biomass_tonnes": audit.get("total_biomass_tonnes"),
            "credits_issued": audit.get("credits_issued"),
            "tx_hash": audit.get("tx_hash"),
            "ipfs_certificate_url": to_gateway_url(
                audit.get("ipfs_metadata_cid") or audit.get("ipfs_url")
            ),
            "audit_year": audit.get("audit_year"),
        }

    if current_status == "COMPLETE_NO_CREDITS":
        return {
            "status": "COMPLETE_NO_CREDITS",
            "total_biomass_tonnes": audit.get("total_biomass_tonnes"),
            "credits_issued": audit.get("credits_issued") or 0,
            "audit_year": audit.get("audit_year"),
            "reason": audit.get("reason") or "No biomass growth detected compared to the latest prior successful audit",
        }

    if current_status in ("CALCULATING", "PROCESSING", "READY_TO_MINT"):
        try:
            zones = await list_sampling_zones_for_audit(audit_id)
            tree_scans = await list_tree_scans_for_audit(audit_id)
        except Exception as exc:
            logger.error("Failed to load audit progress details for %s: %s", audit_id, exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to load audit progress.",
            ) from exc

        return _build_processing_result_payload(current_status, audit, zones, tree_scans)

    if current_status == "FAILED":
        return {
            "status": "FAILED",
            "error": audit.get("error", "Unknown error"),
        }

    logger.error(
        "Audit %s has unsupported status '%s'; returning FAILED to preserve polling contract.",
        audit_id,
        current_status,
    )
    return {
        "status": "FAILED",
        "error": f"Unsupported audit status '{current_status}' encountered.",
    }


@router.get(
    "/history/{land_id}",
    response_model=AuditHistoryResponse,
    status_code=status.HTTP_200_OK,
)
def get_audit_history(
    land_id: str,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Return full audit history for a specific land parcel."""
    try:
        land_resp = (
            supabase_client.table("land_parcels")
            .select("id, user_id")
            .eq("id", land_id)
            .maybe_single()
            .execute()
        )
        land_data = land_resp.data
    except Exception as exc:
        logger.error("Failed to load land %s for audit history: %s", land_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load land parcel.",
        ) from exc

    if not land_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Land parcel '{land_id}' not found.",
        )

    if land_data.get("user_id") != current_user["id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not own this land parcel.",
        )

    try:
        audits_resp = (
            supabase_client.table("carbon_audits")
            .select("audit_year, status, total_biomass_tonnes, credits_issued, tx_hash, ipfs_metadata_cid, ipfs_url, minted_at")
            .eq("land_id", land_id)
            .order("audit_year", desc=True)
            .execute()
        )

        items = [
            AuditHistoryItem(
                audit_year=audit.get("audit_year", 0),
                total_biomass_tonnes=audit.get("total_biomass_tonnes"),
                credits_issued=audit.get("credits_issued"),
                tx_hash=audit.get("tx_hash"),
                ipfs_certificate_url=to_gateway_url(
                    audit.get("ipfs_metadata_cid") or audit.get("ipfs_url")
                ),
                minted_at=audit.get("minted_at"),
            )
            for audit in audits_resp.data or []
            if audit.get("status") in {"MINTED", "COMPLETE_NO_CREDITS"}
        ]

        total = len(items)
        start_index = (page - 1) * limit
        return AuditHistoryResponse(
            items=items[start_index : start_index + limit],
            page=page,
            limit=limit,
            total=total,
            has_more=start_index + limit < total,
        )
    except Exception as exc:
        logger.error("Failed to fetch audit history for land %s: %s", land_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch audit history.",
        ) from exc
