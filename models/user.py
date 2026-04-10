"""Pydantic schemas for backend authentication and user profile APIs."""

from typing import Literal

from pydantic import BaseModel, Field


class KYCRequest(BaseModel):
    """Request body for KYC verification."""

    full_name: str = Field(..., description="Legal full name")
    aadhaar_number: str = Field(..., description="12-digit Aadhaar number")


class AuthMeResponse(BaseModel):
    """Documented profile bootstrap response for ``GET /api/v1/auth/me``."""

    user_id: str
    firebase_uid: str
    phone_number: str | None = None
    full_name: str | None = None
    wallet_address: str | None = None
    kyc_completed: bool = False
    wallet_recovery_status: Literal["PENDING", "APPROVED", "REJECTED"] | None = None
    wallet_recovery_requested_at: str | None = None


class KYCResponse(BaseModel):
    """Standard success response for KYC completion."""

    status: str = "success"
    user_id: str


class WalletRegisterRequest(BaseModel):
    """Request body used after the mobile app creates a farmer wallet."""

    wallet_address: str = Field(..., description="Public Polygon wallet address")


class WalletRegisterResponse(BaseModel):
    """Standard success response for wallet registration."""

    status: str = "success"


class WalletRecoveryRequest(BaseModel):
    """Request body for admin-assisted wallet recovery."""

    new_wallet_address: str = Field(..., description="Replacement Polygon wallet address")


class WalletRecoveryResponse(BaseModel):
    """Pending response for wallet recovery requests."""

    status: str = "pending"
    message: str = "Wallet recovery request submitted"
