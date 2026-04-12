"""Land verification router aligned to the backend design document."""

from datetime import datetime, timezone
from difflib import SequenceMatcher
import json
import logging
from pathlib import Path
from redis import Redis
import threading
import time
import unicodedata
import uuid
from typing import Any, Dict, List
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.database import (
    analyse_boundary_geojson,
    insert_land_parcel_record,
    list_land_parcels_for_user,
    supabase_client,
)
from app.dependencies import get_current_user
from app.rate_limit import RateLimitSpec, enforce_rate_limit
from app.redis_utils import redis_from_url
from models.land import (
    BoundaryFetchResponse,
    DocumentUploadResponse,
    LandListItem,
    LandListResponse,
    LandRegisterRequest,
    LandRegisterResponse,
    LandUpdateRequest,
    LandUpdateResponse,
)
from services import land_boundary_service, ocr_service, satellite_service

logger = logging.getLogger("terratrust.land")

router = APIRouter()

MAX_DOCUMENT_SIZE_BYTES = 10 * 1024 * 1024
ALLOWED_DOCUMENT_CONTENT_TYPES = {"image/jpeg", "image/jpg", "image/png"}
ALLOWED_DOCUMENT_EXTENSIONS = {".jpg", ".jpeg", ".png"}
PENDING_LAND_CONTEXT_TTL_SECONDS = 24 * 60 * 60
OWNER_NAME_MATCH_THRESHOLD = 0.80
VERIFY_DOCUMENT_RATE_LIMIT = RateLimitSpec(
    scope="land.verify-document",
    limit=10,
    window_seconds=60 * 60,
    error_message="Too many land-document verification requests. Please try again later.",
)
FETCH_BOUNDARY_RATE_LIMIT = RateLimitSpec(
    scope="land.fetch-boundary",
    limit=20,
    window_seconds=60 * 60,
    error_message="Too many boundary fetch requests. Please try again later.",
)

_pending_land_context_client: Redis | None = None
_pending_land_context_initialised = False
_pending_land_context_lock = threading.Lock()
_pending_land_context_memory: dict[str, tuple[float, Dict[str, Any]]] = {}


# ---------------------------------------------------------------------------
# Helper: normalise names for comparison
# ---------------------------------------------------------------------------
def _normalise_name(name: str) -> str:
    """Lower-case, strip accents / diacritics, collapse whitespace."""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(ascii_only.lower().split())


def _name_similarity(left: str, right: str) -> float:
    """Return a fuzzy similarity ratio for owner-name matching."""
    return SequenceMatcher(None, _normalise_name(left), _normalise_name(right)).ratio()


def _normalise_geojson_for_compare(geojson: Dict[str, Any]) -> str:
    """Return a stable serialised form for server-side geometry comparison."""
    return json.dumps(geojson, sort_keys=True, separators=(",", ":"))


def _ensure_kyc_completed(current_user: Dict[str, Any]) -> None:
    """Enforce the documented KYC prerequisite for land registration flows."""
    if current_user.get("kyc_completed") and current_user.get("full_name"):
        return

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="KYC must be completed before registering land.",
    )


def _ensure_owner_name_matches_kyc(owner_name: str, current_user: Dict[str, Any]) -> None:
    """Reject land-document owners that do not match the authenticated KYC profile."""
    _ensure_kyc_completed(current_user)

    similarity = _name_similarity(owner_name, current_user.get("full_name") or "")
    if similarity < OWNER_NAME_MATCH_THRESHOLD:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Owner name on document does not match your registered name.",
        )


def _ensure_pending_context_matches(
    label: str,
    expected_value: str | None,
    received_value: str,
) -> None:
    """Reject client fields that differ from the verified server-side registration flow."""
    if expected_value is None:
        return

    if _normalise_name(expected_value) == _normalise_name(received_value):
        return

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"{label} does not match the verified land document.",
    )


def _validate_pending_registration_context(
    body: LandRegisterRequest,
    pending_context: Dict[str, Any],
    normalized_geojson: Dict[str, Any],
) -> None:
    """Ensure registration uses the verified OCR and boundary-fetch context."""
    if not pending_context.get("owner_name") or not pending_context.get("doc_image_url"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Land document must be verified before registering land.",
        )

    _ensure_pending_context_matches("Owner name", pending_context.get("owner_name"), body.ocr_owner_name)
    _ensure_pending_context_matches("District", pending_context.get("district"), body.district)
    _ensure_pending_context_matches("Taluka", pending_context.get("taluka"), body.taluka)
    _ensure_pending_context_matches("Village", pending_context.get("village"), body.village)
    _ensure_pending_context_matches("State", pending_context.get("state"), body.state)

    cached_boundary_source = pending_context.get("boundary_source")
    cached_boundary_geojson = pending_context.get("boundary_geojson")
    if cached_boundary_source and cached_boundary_source != body.boundary_source:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Boundary source does not match the verified land registration flow.",
        )

    if not cached_boundary_geojson:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Land boundary must be fetched and confirmed before registration.",
        )

    if _normalise_geojson_for_compare(cached_boundary_geojson) != _normalise_geojson_for_compare(normalized_geojson):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Submitted land boundary does not match the verified government boundary.",
        )


def _get_pending_land_context_client() -> Redis | None:
    """Return the Redis client used to bridge document and boundary state."""
    global _pending_land_context_client, _pending_land_context_initialised

    if _pending_land_context_initialised:
        return _pending_land_context_client

    _pending_land_context_initialised = True
    try:
        _pending_land_context_client = redis_from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        _pending_land_context_client.ping()
    except Exception as exc:
        logger.warning("Redis unavailable for pending land context caching: %s", exc)
        _pending_land_context_client = None

    return _pending_land_context_client


def _pending_land_context_key(user_id: str, survey_number: str) -> str:
    """Build the Redis key used for a user's in-flight land registration."""
    return f"pending-land-context:{user_id}:{survey_number.strip().lower()}"


def _load_pending_context_payload(raw_value: str | None) -> Dict[str, Any]:
    """Decode cached pending-context JSON safely."""
    if not raw_value:
        return {}

    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        logger.warning("Discarding invalid pending land context payload.")
        return {}

    return payload if isinstance(payload, dict) else {}


def _get_memory_pending_land_context(key: str) -> Dict[str, Any]:
    """Return the in-memory fallback payload for a pending land registration."""
    now = time.time()
    with _pending_land_context_lock:
        cached = _pending_land_context_memory.get(key)
        if not cached:
            return {}

        expires_at, payload = cached
        if now >= expires_at:
            _pending_land_context_memory.pop(key, None)
            return {}

        return dict(payload)


def _set_memory_pending_land_context(key: str, payload: Dict[str, Any]) -> None:
    """Persist pending land context in process memory when Redis is unavailable."""
    expires_at = time.time() + PENDING_LAND_CONTEXT_TTL_SECONDS
    with _pending_land_context_lock:
        cached = _pending_land_context_memory.get(key)
        existing_payload = {}
        if cached:
            cached_expires_at, cached_payload = cached
            if time.time() < cached_expires_at:
                existing_payload = dict(cached_payload)
            else:
                _pending_land_context_memory.pop(key, None)
        merged_payload = {
            **existing_payload,
            **{field: value for field, value in payload.items() if value is not None},
        }
        _pending_land_context_memory[key] = (expires_at, merged_payload)


def _delete_memory_pending_land_context(key: str) -> None:
    """Delete the in-memory pending-context fallback state."""
    with _pending_land_context_lock:
        _pending_land_context_memory.pop(key, None)


def _cache_pending_land_context(user_id: str, survey_number: str, payload: Dict[str, Any]) -> None:
    """Merge partial document or boundary metadata into Redis."""
    key = _pending_land_context_key(user_id, survey_number)
    redis_client = _get_pending_land_context_client()

    if redis_client is None:
        _set_memory_pending_land_context(key, payload)
        return

    try:
        existing_payload = _load_pending_context_payload(redis_client.get(key))
        merged_payload = {
            **existing_payload,
            **{field: value for field, value in payload.items() if value is not None},
        }
        redis_client.setex(key, PENDING_LAND_CONTEXT_TTL_SECONDS, json.dumps(merged_payload))
        _delete_memory_pending_land_context(key)
    except Exception as exc:
        logger.warning(
            "Falling back to in-memory pending land context cache for %s: %s",
            key,
            exc,
        )
        _set_memory_pending_land_context(key, payload)


def _get_pending_land_context(user_id: str, survey_number: str) -> Dict[str, Any]:
    """Return cached document and boundary metadata for a survey number."""
    key = _pending_land_context_key(user_id, survey_number)
    redis_client = _get_pending_land_context_client()
    if redis_client is None:
        return _get_memory_pending_land_context(key)

    try:
        cached = redis_client.get(key)
    except Exception as exc:
        logger.warning(
            "Falling back to in-memory pending land context lookup for %s: %s",
            key,
            exc,
        )
        return _get_memory_pending_land_context(key)

    payload = _load_pending_context_payload(cached)
    if payload:
        return payload
    return _get_memory_pending_land_context(key)


def _clear_pending_land_context(user_id: str, survey_number: str) -> None:
    """Remove cached in-flight land registration state after success."""
    key = _pending_land_context_key(user_id, survey_number)
    _delete_memory_pending_land_context(key)

    redis_client = _get_pending_land_context_client()
    if redis_client is None:
        return

    try:
        redis_client.delete(key)
    except Exception as exc:
        logger.warning("Failed to clear pending land context %s from Redis: %s", key, exc)


def _authenticated_storage_object_url(bucket: str, object_path: str) -> str:
    """Return the canonical authenticated URL for a private Supabase object."""
    encoded_path = quote(object_path, safe="/")
    return (
        f"{settings.SUPABASE_URL.rstrip('/')}"
        f"/storage/v1/object/authenticated/{bucket}/{encoded_path}"
    )


def _validate_document_upload(file: UploadFile, image_bytes: bytes) -> None:
    """Validate documented JPG/PNG image constraints for OCR and manual map flows."""
    content_type = (file.content_type or "").lower()
    extension = Path(file.filename or "").suffix.lower()

    if (
        content_type not in ALLOWED_DOCUMENT_CONTENT_TYPES
        and extension not in ALLOWED_DOCUMENT_EXTENSIONS
    ):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only JPG and PNG land-document images are supported.",
        )

    if not image_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded land document is empty.",
        )

    if len(image_bytes) > MAX_DOCUMENT_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Land document image must be 10 MB or smaller.",
        )


# ---------------------------------------------------------------------------
# POST /verify-document
# ---------------------------------------------------------------------------
@router.post(
    "/verify-document",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_200_OK,
)
async def verify_document(
    image: UploadFile | None = File(None, description="Scanned land document image"),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Extract structured fields from a scanned land document using OCR."""
    _ensure_kyc_completed(current_user)
    enforce_rate_limit(current_user["id"], VERIFY_DOCUMENT_RATE_LIMIT)

    if image is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Multipart field 'image' is required.",
        )

    try:
        image_bytes = await image.read()
        _validate_document_upload(image, image_bytes)
        fields = await run_in_threadpool(ocr_service.extract_fields_from_document, image_bytes)
        _ensure_owner_name_matches_kyc(fields["owner_name"], current_user)

        document_extension = Path(image.filename or "").suffix.lower() or ".jpg"
        if document_extension not in ALLOWED_DOCUMENT_EXTENSIONS:
            document_extension = ".jpg"

        document_storage_path = (
            f"{current_user['id']}/pending/{uuid.uuid4()}{document_extension}"
        )
        await run_in_threadpool(
            lambda: supabase_client.storage.from_("land-documents").upload(
                document_storage_path,
                image_bytes,
            )
        )

        _cache_pending_land_context(
            current_user["id"],
            fields["survey_number"],
            {
                "doc_image_url": _authenticated_storage_object_url(
                    "land-documents",
                    document_storage_path,
                ),
                "owner_name": fields.get("owner_name"),
                "village": fields.get("village"),
                "taluka": fields.get("taluka"),
                "district": fields.get("district"),
                "state": fields.get("state", "Maharashtra"),
            },
        )

        return DocumentUploadResponse(
            survey_number=fields["survey_number"],
            owner_name=fields["owner_name"],
            village=fields.get("village"),
            taluka=fields.get("taluka"),
            district=fields.get("district"),
            state=fields.get("state", "Maharashtra"),
            extraction_confidence=fields["extraction_confidence"],
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.error("Document verification failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Document processing failed.",
        ) from exc


# ---------------------------------------------------------------------------
# GET /fetch-boundary
# ---------------------------------------------------------------------------
@router.get("/fetch-boundary", response_model=BoundaryFetchResponse)
async def fetch_boundary(
    survey_number: str = Query(...),
    district: str = Query(...),
    taluka: str = Query(...),
    village: str = Query(...),
    state: str = Query(...),
    user_lat: float = Query(...),
    user_lng: float = Query(...),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Attempt to auto-fetch the land boundary from government GIS layers."""
    _ensure_kyc_completed(current_user)
    enforce_rate_limit(current_user["id"], FETCH_BOUNDARY_RATE_LIMIT)

    result = await land_boundary_service.fetch_land_boundary(
        survey_number=survey_number,
        district=district,
        taluka=taluka,
        village=village,
        state=state,
        user_lat=user_lat,
        user_lng=user_lng,
    )

    if result.get("status") == "success":
        _cache_pending_land_context(
            current_user["id"],
            survey_number,
            {
                "boundary_source": result.get("boundary_source"),
                "lgd_district_code": result.get("lgd_district_code"),
                "lgd_taluka_code": result.get("lgd_taluka_code"),
                "lgd_village_code": result.get("lgd_village_code"),
                "gis_code": result.get("gis_code"),
                "boundary_geojson": result.get("geojson"),
            },
        )

    return BoundaryFetchResponse(**result)


@router.post("/fetch-boundary", response_model=BoundaryFetchResponse)
async def fetch_boundary_from_manual_map(
    map_image: UploadFile | None = File(None, description="Downloaded government parcel map image"),
    survey_number: str = Form(...),
    district: str = Form(...),
    taluka: str = Form(...),
    village: str = Form(...),
    state: str = Form(...),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Process a farmer-uploaded government map image into a parcel boundary."""
    _ensure_kyc_completed(current_user)
    enforce_rate_limit(current_user["id"], FETCH_BOUNDARY_RATE_LIMIT)

    if map_image is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Multipart field 'map_image' is required.",
        )

    try:
        image_bytes = await map_image.read()
        _validate_document_upload(map_image, image_bytes)
        result = await land_boundary_service.process_manual_boundary_map(
            image_bytes=image_bytes,
            survey_number=survey_number,
            district=district,
            taluka=taluka,
            village=village,
            state=state,
        )
        _cache_pending_land_context(
            current_user["id"],
            survey_number,
            {
                "district": district,
                "taluka": taluka,
                "village": village,
                "state": state,
                "boundary_source": result.get("boundary_source"),
                "boundary_geojson": result.get("geojson"),
            },
        )
        return BoundaryFetchResponse(**result)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Manual boundary processing failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Manual boundary processing failed.",
        ) from exc


# ---------------------------------------------------------------------------
# POST /register
# ---------------------------------------------------------------------------
@router.post(
    "/register",
    response_model=LandRegisterResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_land(
    body: LandRegisterRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Register a verified land parcel.

    Validations
    -----------
    1. OCR owner name must match KYC name (normalised comparison).
    2. GeoJSON must be valid polygonal geometry in PostGIS.
    3. No duplicate (same user + survey_number).
    """

    # --- 1. Cross-check owner name with KYC name --------------------------
    _ensure_owner_name_matches_kyc(body.ocr_owner_name, current_user)

    # --- 2. Validate GeoJSON in PostGIS ------------------------------------
    try:
        boundary_analysis = await analyse_boundary_geojson(body.geojson)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid GeoJSON geometry: {exc}",
        ) from exc

    if not boundary_analysis.get("is_valid"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid GeoJSON geometry.",
        )

    geometry_type = boundary_analysis.get("geometry_type")
    if geometry_type not in {"ST_Polygon", "ST_MultiPolygon"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Land boundary must be a Polygon or MultiPolygon geometry.",
        )

    # --- 3. Duplicate check ------------------------------------------------
    dup_check = await run_in_threadpool(
        lambda: (
            supabase_client.table("land_parcels")
            .select("id")
            .eq("user_id", current_user["id"])
            .eq("survey_number", body.survey_number)
            .execute()
        )
    )
    if dup_check.data:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This survey number is already registered under your account.",
        )

    area_hectares = round(float(boundary_analysis.get("area_hectares") or 0), 4)

    if area_hectares < 0.1 or area_hectares > 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Land area must be between 0.1 and 100 hectares.",
        )

    pending_context = _get_pending_land_context(current_user["id"], body.survey_number)
    _validate_pending_registration_context(
        body,
        pending_context,
        boundary_analysis.get("normalized_geojson") or body.geojson,
    )

    # --- 4. Insert into Supabase -------------------------------------------
    land_id = str(uuid.uuid4())
    insert_data: Dict[str, Any] = {
        "id": land_id,
        "user_id": current_user["id"],
        "farm_name": body.farm_name,
        "survey_number": body.survey_number,
        "district": body.district,
        "taluka": body.taluka,
        "village": body.village,
        "state": body.state,
        "boundary_source": body.boundary_source,
        "ocr_owner_name": body.ocr_owner_name,
        "boundary_geojson": boundary_analysis.get("normalized_geojson") or body.geojson,
        "is_verified": True,
        "doc_image_url": pending_context.get("doc_image_url"),
        "lgd_district_code": pending_context.get("lgd_district_code"),
        "lgd_taluka_code": pending_context.get("lgd_taluka_code"),
        "lgd_village_code": pending_context.get("lgd_village_code"),
        "gis_code": pending_context.get("gis_code"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        insert_result = await insert_land_parcel_record(insert_data)
    except Exception as exc:
        logger.error("Failed to insert land parcel: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to register land parcel.",
        ) from exc

    _clear_pending_land_context(current_user["id"], body.survey_number)

    logger.info("Land parcel %s registered for user %s", land_id, current_user["id"])
    return LandRegisterResponse(
        land_id=land_id,
        area_hectares=insert_result.get("area_hectares") or area_hectares,
        status="verified",
    )


@router.get("/list", response_model=LandListResponse, status_code=status.HTTP_200_OK)
async def list_lands(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Return all registered land parcels for the authenticated farmer."""
    try:
        parcels = await list_land_parcels_for_user(current_user["id"])
        total = len(parcels)
        start_index = (page - 1) * limit
        page_items = parcels[start_index : start_index + limit]
        current_year = datetime.now(timezone.utc).year

        items: List[LandListItem] = []
        for parcel in page_items:
            audits_response = (
                await run_in_threadpool(
                    lambda parcel_id=parcel["id"]: (
                        supabase_client.table("carbon_audits")
                        .select("id, status, audit_year, created_at")
                        .eq("land_id", parcel_id)
                        .order("audit_year", desc=True)
                        .execute()
                    )
                )
            )
            audit_rows = audits_response.data or []
            last_audit_year = next(
                (
                    audit["audit_year"]
                    for audit in audit_rows
                    if audit.get("status") in {"MINTED", "COMPLETE_NO_CREDITS"}
                ),
                None,
            )
            current_audit = next(
                (audit for audit in audit_rows if audit.get("audit_year") == current_year),
                None,
            )

            thumbnail_url = None
            try:
                boundary_geojson = parcel.get("boundary_geojson") or parcel.get("geojson")
                if boundary_geojson:
                    thumbnail_url = await run_in_threadpool(
                        satellite_service.generate_true_color_thumbnail_url,
                        boundary_geojson,
                        512,
                    )
            except Exception as exc:
                logger.warning("Failed to generate thumbnail for land %s: %s", parcel["id"], exc)

            items.append(
                LandListItem(
                    id=parcel["id"],
                    farm_name=parcel.get("farm_name") or "Unnamed Field",
                    survey_number=parcel["survey_number"],
                    district=parcel["district"],
                    taluka=parcel["taluka"],
                    village=parcel["village"],
                    state=parcel.get("state") or "Maharashtra",
                    area_hectares=parcel.get("area_hectares") or 0.0,
                    is_verified=bool(parcel.get("is_verified", True)),
                    boundary_source=parcel.get("boundary_source"),
                    registered_at=parcel.get("registered_at"),
                    last_audit_year=last_audit_year,
                    current_audit_id=current_audit.get("id") if current_audit else None,
                    current_audit_status=current_audit.get("status") if current_audit else None,
                    thumbnail_url=thumbnail_url,
                )
            )

        return LandListResponse(
            items=items,
            page=page,
            limit=limit,
            total=total,
            has_more=start_index + limit < total,
        )
    except Exception as exc:
        logger.error("Failed to list lands for user %s: %s", current_user["id"], exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch registered lands.",
        ) from exc


@router.patch("/{land_id}", response_model=LandUpdateResponse, status_code=status.HTTP_200_OK)
def update_land_name(
    land_id: str,
    body: LandUpdateRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Update the farmer-facing name of a registered land parcel."""
    farm_name = " ".join(body.farm_name.split())
    if not farm_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Farm name cannot be empty.",
        )

    try:
        land_response = (
            supabase_client.table("land_parcels")
            .select("id, user_id")
            .eq("id", land_id)
            .maybe_single()
            .execute()
        )
        land_data = land_response.data
    except Exception as exc:
        logger.error("Failed to load land parcel %s for rename: %s", land_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load land parcel.",
        ) from exc

    if not land_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Land parcel not found",
        )

    if land_data.get("user_id") != current_user["id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not own this land parcel.",
        )

    supabase_client.table("land_parcels").update(
        {
            "farm_name": farm_name,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", land_id).execute()

    return LandUpdateResponse(land_id=land_id, farm_name=farm_name)
