"""Application configuration and environment loading."""

import os
import re
from typing import Literal, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EVM_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
HEX_PRIVATE_KEY_RE = re.compile(r"^0x[a-fA-F0-9]{64}$")
HEX_PRIVATE_KEY_WITH_OPTIONAL_PREFIX_RE = re.compile(r"^(?:0x)?[a-fA-F0-9]{64}$")


class Settings(BaseSettings):
    """TerraTrust-AR application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )

    # --- Firebase Authentication -------------------------------------------
    FIREBASE_PROJECT_ID: str

    # --- Supabase -----------------------------------------------------------
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str
    DATABASE_URL: Optional[str] = None

    # --- Google integrations -----------------------------------------------
    FIREBASE_CREDENTIALS_PATH: str = ""
    GEE_PROJECT_ID: str = ""
    GEE_CREDENTIALS_PATH: str = ""
    CLOUD_VISION_CREDENTIALS_PATH: str = ""

    # --- Legacy shared Google fallback -------------------------------------
    GOOGLE_CLOUD_PROJECT: str = ""
    GOOGLE_APPLICATION_CREDENTIALS: str = ""

    # --- NASA NISAR ---------------------------------------------------------
    NASA_EARTHDATA_USERNAME: str = ""
    NASA_EARTHDATA_PASSWORD: str = ""
    NISAR_PRODUCTION_READY: bool = False

    # --- Blockchain ---------------------------------------------------------
    ADMIN_WALLET_PRIVATE_KEY: str = ""
    ADMIN_WALLET_ADDRESS: str = ""
    ALCHEMY_POLYGON_AMOY_URL: str = ""
    CONTRACT_ADDRESS: str = ""

    # --- IPFS / Pinata ------------------------------------------------------
    PINATA_JWT: str = ""
    PINATA_GATEWAY_URL: str = ""

    # --- PolygonScan --------------------------------------------------------
    POLYGONSCAN_API_KEY: str = ""

    # --- Government APIs ----------------------------------------------------
    LGD_API_BASE: str = "http://115.124.105.220/API"

    # --- Redis --------------------------------------------------------------
    REDIS_URL: str = "redis://localhost:6379/0"

    # --- App ----------------------------------------------------------------
    ENVIRONMENT: Literal["development", "production"] = "development"
    WEB_CORS_ORIGINS: str = ""
    MAINTENANCE_MODE: bool = False
    MAINTENANCE_MESSAGE: Optional[str] = None

    @field_validator("ADMIN_WALLET_ADDRESS", "CONTRACT_ADDRESS")
    @classmethod
    def _validate_evm_address(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate:
            return ""
        if not EVM_ADDRESS_RE.fullmatch(candidate):
            raise ValueError("must be a valid 0x-prefixed EVM address")
        return candidate

    @field_validator("ADMIN_WALLET_PRIVATE_KEY")
    @classmethod
    def _validate_private_key(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate:
            return ""

        normalised = candidate if candidate.startswith("0x") else f"0x{candidate}"
        if not HEX_PRIVATE_KEY_WITH_OPTIONAL_PREFIX_RE.fullmatch(candidate) or not HEX_PRIVATE_KEY_RE.fullmatch(normalised):
            raise ValueError("must be a valid 0x-prefixed 32-byte hex private key")
        return normalised

    @field_validator("PINATA_GATEWAY_URL")
    @classmethod
    def _normalise_pinata_gateway(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate:
            return ""
        candidate = candidate.removeprefix("https://").removeprefix("http://")
        return candidate.rstrip("/")


# Singleton — import ``settings`` from anywhere in the app.
settings = Settings()  # type: ignore[call-arg]

# -- Demo account production safety guard ------------------------------
if (
    os.getenv("ENVIRONMENT", "development") == "production"
    and os.getenv("ENABLE_DEMO_ACCOUNTS", "false").lower() == "true"
):
    raise RuntimeError(
        "\n\nCRITICAL SECURITY ERROR:\n"
        "ENABLE_DEMO_ACCOUNTS=true is set in production.\n"
        "Demo phone numbers (+91 9000000001 etc.) are short and guessable.\n"
        "Set ENABLE_DEMO_ACCOUNTS=false in production .env and redeploy.\n"
    )
# ---------------------------------------------------------------------
