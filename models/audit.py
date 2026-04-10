"""Pydantic schemas for audit endpoints and AR sample payloads."""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class GPSPoint(BaseModel):
    """A single GPS coordinate."""

    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)


class TreeSample(BaseModel):
    """One AR-scanned tree sample submitted by the farmer."""

    zone_id: str
    species: str
    species_confidence: float = Field(..., ge=0.0, le=1.0)
    species_source: Literal["MODEL_AUTO", "MODEL_CONFIRMED", "MANUAL_SELECTED"]
    dbh_cm: float = Field(
        ...,
        gt=0,
        lt=500,
        description="Diameter at breast height in cm",
    )
    height_m: Optional[float] = Field(None, gt=0, description="Height in metres (optional)")
    gps: GPSPoint
    gps_accuracy_m: float = Field(..., ge=0, description="Horizontal GPS accuracy in metres")
    ar_tier_used: Literal[1, 2, 3] = Field(
        ..., description="AR measurement tier: 1=full depth, 2=SLAM, 3=manual"
    )
    confidence_score: Optional[float] = Field(None, ge=0.0, le=1.0)
    evidence_photo_base64: str = Field(..., description="Base64-encoded photo evidence")
    evidence_photo_hash: str = Field(..., description="SHA-256 hash of the photo bytes")
    scan_timestamp: datetime


class AuditSubmitRequest(BaseModel):
    """Request body to submit tree scan samples for an audit."""

    land_id: str
    audit_id: str
    trees: List[TreeSample] = Field(..., min_length=1)


class AuditSubmitResponse(BaseModel):
    """Acknowledgement after accepting tree samples."""

    status: Literal["PROCESSING"]
    audit_id: str
    estimated_seconds: int = 60
    message: str = "Satellite verification in progress"


class AuditResultProcessingResponse(BaseModel):
    """Non-terminal audit status payload returned to the polling client."""

    status: Literal["PROCESSING", "CALCULATING", "READY_TO_MINT"]


class AuditResultMintedResponse(BaseModel):
    """Terminal response returned after successful minting."""

    status: Literal["MINTED"]
    total_biomass_tonnes: float | None = None
    credits_issued: float | None = None
    tx_hash: str | None = None
    ipfs_certificate_url: str | None = None
    audit_year: int | None = None


class AuditResultNoCreditsResponse(BaseModel):
    """Terminal response returned when an audit completes without growth."""

    status: Literal["COMPLETE_NO_CREDITS"]
    total_biomass_tonnes: float | None = None
    credits_issued: float = 0
    audit_year: int | None = None
    reason: str


class AuditResultFailedResponse(BaseModel):
    """Terminal response returned when audit processing fails."""

    status: Literal["FAILED"]
    error: str


AuditResultResponse = (
    AuditResultProcessingResponse
    | AuditResultMintedResponse
    | AuditResultNoCreditsResponse
    | AuditResultFailedResponse
)


class ZoneResponse(BaseModel):
    """A single sampling zone returned to the mobile app."""

    zone_id: str
    label: str = Field(..., description="Human label e.g. A, B, C")
    centre_gps: GPSPoint
    radius_metres: float
    zone_type: Literal["high_density", "medium_density", "low_density"] = Field(
        ..., description="NDVI category: high_density / medium_density / low_density"
    )
    sequence_order: int
    gedi_available: bool


class AuditZonesResponse(BaseModel):
    """Response containing all sampling zones for an audit."""

    audit_id: str
    zones: List[ZoneResponse]
    walking_path_metres: float
    min_trees_required: int


class AuditHistoryItem(BaseModel):
    """A single historical audit record for a land parcel."""

    audit_year: int
    total_biomass_tonnes: float | None = None
    credits_issued: float | None = None
    tx_hash: str | None = None
    ipfs_certificate_url: str | None = None
    minted_at: str | None = None


class AuditHistoryResponse(BaseModel):
    """Paginated audit-history payload for a single land parcel."""

    items: List[AuditHistoryItem]
    page: int
    limit: int
    total: int
    has_more: bool
