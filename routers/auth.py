"""Authentication router aligned to the backend design document."""

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status
from web3 import Web3

from app.database import supabase_client
from app.dependencies import get_current_user
from models.user import (
    AuthMeResponse,
    KYCRequest,
    KYCResponse,
    WalletRecoveryRequest,
    WalletRecoveryResponse,
    WalletRegisterRequest,
    WalletRegisterResponse,
)

logger = logging.getLogger("terratrust.auth")

router = APIRouter()

AADHAAR_RE = re.compile(r"^\d{12}$")
WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def _same_wallet_address(left: str, right: str) -> bool:
    """Compare EVM wallet addresses case-insensitively."""
    return left.strip().lower() == right.strip().lower()


def _validate_aadhaar_number(aadhaar_number: str) -> str:
    """Validate Aadhaar format and raise the documented 400 response on failure."""
    candidate = aadhaar_number.strip()
    if not AADHAAR_RE.fullmatch(candidate):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Aadhaar number format",
        )
    return candidate


def _validate_full_name(full_name: str) -> str:
    """Validate the documented KYC full-name rules."""
    candidate = " ".join(full_name.split())
    if len(candidate) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Full name must be at least 2 characters.",
        )
    if not all(character.isalpha() or character.isspace() for character in candidate):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Full name must contain only letters and spaces.",
        )
    return candidate


def _validate_wallet_address(wallet_address: str) -> str:
    """Validate wallet format and raise the documented 400 response on failure."""
    candidate = wallet_address.strip()
    if not WALLET_RE.fullmatch(candidate) or not Web3.is_address(candidate):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid wallet address format",
        )
    return Web3.to_checksum_address(candidate)


def _build_auth_me_response(current_user: Dict[str, Any]) -> AuthMeResponse:
    """Convert a database user row into the documented response shape."""
    return AuthMeResponse(
        user_id=current_user["id"],
        firebase_uid=current_user["firebase_uid"],
        phone_number=current_user.get("phone_number"),
        full_name=current_user.get("full_name"),
        kyc_completed=bool(current_user.get("kyc_completed", False)),
        wallet_address=current_user.get("wallet_address"),
        wallet_recovery_status=current_user.get("wallet_recovery_status"),
        wallet_recovery_requested_at=current_user.get("wallet_recovery_requested_at"),
    )


@router.get("/me", response_model=AuthMeResponse, status_code=status.HTTP_200_OK)
def get_me(current_user: Dict[str, Any] = Depends(get_current_user)):
    """Return the authenticated farmer profile state for mobile bootstrap."""
    return _build_auth_me_response(current_user)


@router.post("/kyc", response_model=KYCResponse, status_code=status.HTTP_200_OK)
def submit_kyc(
    body: KYCRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Complete KYC for the authenticated user.

    - Hashes the Aadhaar number with SHA-256 (plain text is **never** stored).
    - Stores full_name and aadhaar_hash in the ``users`` table.
    - Sets ``kyc_completed = true``.
    """
    try:
        full_name = _validate_full_name(body.full_name)

        aadhaar_number = _validate_aadhaar_number(body.aadhaar_number)
        aadhaar_hash = hashlib.sha256(aadhaar_number.encode("utf-8")).hexdigest()

        update_data = {
            "full_name": full_name,
            "aadhaar_hash": aadhaar_hash,
            "kyc_completed": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        (
            supabase_client.table("users")
            .update(update_data)
            .eq("id", current_user["id"])
            .execute()
        )

        logger.info("KYC completed for user %s", current_user["id"])
        return KYCResponse(user_id=current_user["id"])

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("KYC submission failed for user %s: %s", current_user["id"], exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="KYC processing failed. Please try again.",
        ) from exc


@router.post(
    "/register-wallet",
    response_model=WalletRegisterResponse,
    status_code=status.HTTP_200_OK,
)
def register_wallet(
    body: WalletRegisterRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Store the farmer's public wallet address after silent wallet creation."""
    try:
        wallet_address = _validate_wallet_address(body.wallet_address)
        current_wallet = current_user.get("wallet_address")
        if current_wallet:
            if _same_wallet_address(current_wallet, wallet_address):
                return WalletRegisterResponse()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A wallet address is already registered for this user.",
            )

        existing_wallet = (
            supabase_client.table("users")
            .select("id, wallet_address")
            .ilike("wallet_address", wallet_address)
            .limit(1)
            .execute()
        )
        if existing_wallet.data and existing_wallet.data[0]["id"] != current_user["id"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This wallet address is already registered to another user.",
            )

        (
            supabase_client.table("users")
            .update(
                {
                    "wallet_address": wallet_address,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            .eq("id", current_user["id"])
            .execute()
        )
        logger.info(
            "Wallet registered for user %s: %s",
            current_user["id"],
            wallet_address,
        )
        return WalletRegisterResponse()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Wallet registration failed for user %s: %s", current_user["id"], exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Wallet registration failed. Please try again.",
        ) from exc


@router.post(
    "/recover-wallet",
    response_model=WalletRecoveryResponse,
    status_code=status.HTTP_200_OK,
)
def recover_wallet(
    body: WalletRecoveryRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Create an admin-assisted wallet recovery request."""
    try:
        if not current_user.get("kyc_completed"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="KYC must be completed before requesting wallet recovery.",
            )

        current_wallet = current_user.get("wallet_address")
        if not current_wallet:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No existing wallet address is registered for this user.",
            )

        new_wallet_address = _validate_wallet_address(body.new_wallet_address)
        if _same_wallet_address(current_wallet, new_wallet_address):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="New wallet address must be different from the current wallet.",
            )

        pending_request = (
            supabase_client.table("wallet_recovery_requests")
            .select("id")
            .eq("user_id", current_user["id"])
            .eq("status", "PENDING")
            .limit(1)
            .execute()
        )
        if pending_request.data:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A wallet recovery request is already pending",
            )

        existing_wallet = (
            supabase_client.table("users")
            .select("id")
            .ilike("wallet_address", new_wallet_address)
            .limit(1)
            .execute()
        )
        if existing_wallet.data and existing_wallet.data[0]["id"] != current_user["id"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This wallet address is already registered to another user.",
            )

        supabase_client.table("wallet_recovery_requests").insert(
            {
                "user_id": current_user["id"],
                "old_wallet_address": current_wallet,
                "new_wallet_address": new_wallet_address,
                "status": "PENDING",
                "requested_at": datetime.now(timezone.utc).isoformat(),
            }
        ).execute()

        logger.info(
            "Wallet recovery requested for user %s: %s -> %s",
            current_user["id"],
            current_wallet,
            new_wallet_address,
        )
        return WalletRecoveryResponse()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Wallet recovery request failed for user %s: %s", current_user["id"], exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Wallet recovery request failed. Please try again.",
        ) from exc
