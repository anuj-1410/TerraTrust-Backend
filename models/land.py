"""Pydantic schemas for land verification and parcel listing APIs."""

from datetime import datetime
from typing import Any, Dict, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class DocumentUploadResponse(BaseModel):
    """Response returned after OCR extraction from a land document."""

    survey_number: str
    owner_name: str
    village: Optional[str] = None
    taluka: Optional[str] = None
    district: Optional[str] = None
    state: Optional[str] = None
    extraction_confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Fraction of fields successfully extracted"
    )


class BoundaryFetchResponse(BaseModel):
    """Response for a boundary-fetch attempt."""

    status: Literal["success", "manual_required"]
    boundary_source: Optional[str] = None
    satellite_png_url: Optional[str] = None
    satellite_thumbnail_url: Optional[str] = None
    geojson: Optional[Dict[str, Any]] = None
    area_hectares: Optional[float] = None
    message: Optional[str] = None


class LandRegisterRequest(BaseModel):
    """Request body to register a verified land parcel."""

    farm_name: str = Field(..., min_length=1, max_length=300)
    survey_number: str
    district: str
    taluka: str
    village: str
    state: str
    boundary_source: Literal["WMS_AUTO", "SCRAPE", "MANUAL"]
    geojson: Dict[str, Any] = Field(..., description="GeoJSON geometry of the parcel boundary")
    ocr_owner_name: str = Field(..., description="Owner name as extracted by OCR")


class LandRegisterResponse(BaseModel):
    """Response after successful land registration."""

    land_id: str
    area_hectares: float
    status: Literal["verified"] = "verified"


class LandListItem(BaseModel):
    """Compact parcel representation returned by ``GET /api/v1/land/list``."""

    id: str
    farm_name: str
    survey_number: str
    district: str
    taluka: str
    village: str
    state: str
    area_hectares: float
    is_verified: bool = True
    boundary_source: str | None = None
    registered_at: str | None = None
    last_audit_year: int | None = None
    current_audit_id: str | None = None
    current_audit_status: str | None = None
    thumbnail_url: str | None = None

    @field_validator("id", "current_audit_id", mode="before")
    @classmethod
    def _coerce_uuid_to_str(cls, value: Any) -> Any:
        """Preserve string API output when SQLAlchemy returns native UUID objects."""
        if isinstance(value, UUID):
            return str(value)
        return value

    @field_validator("registered_at", mode="before")
    @classmethod
    def _coerce_datetime_to_iso(cls, value: Any) -> Any:
        """Serialise native datetimes from direct SQL helpers to ISO-8601 strings."""
        if isinstance(value, datetime):
            return value.isoformat()
        return value


class LandListResponse(BaseModel):
    """Paginated parcel list returned by ``GET /api/v1/land/list``."""

    items: list[LandListItem]
    page: int
    limit: int
    total: int
    has_more: bool


class LandUpdateRequest(BaseModel):
    """Request body for renaming a farmer-facing parcel label."""

    farm_name: str = Field(..., min_length=1, max_length=300)


class LandUpdateResponse(BaseModel):
    """Response after updating a parcel's farmer-facing name."""

    status: Literal["success"] = "success"
    land_id: str
    farm_name: str
