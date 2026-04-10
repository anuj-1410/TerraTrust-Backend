"""Helpers for resolving Google credential files per backend integration."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from app.config import settings

GoogleService = Literal["firebase", "gee", "cloud_vision"]

_SERVICE_SETTING_NAMES: dict[GoogleService, str] = {
    "firebase": "FIREBASE_CREDENTIALS_PATH",
    "gee": "GEE_CREDENTIALS_PATH",
    "cloud_vision": "CLOUD_VISION_CREDENTIALS_PATH",
}


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _resolve_relative_path(configured_path: str) -> Path:
    candidate = Path(configured_path)
    if candidate.is_absolute():
        return candidate
    return (_backend_root() / candidate).resolve()


def _credentials_source(service: GoogleService) -> tuple[str, str]:
    setting_name = _SERVICE_SETTING_NAMES[service]
    configured_path = str(getattr(settings, setting_name, "") or "").strip()
    if configured_path:
        return setting_name, configured_path

    legacy_path = str(getattr(settings, "GOOGLE_APPLICATION_CREDENTIALS", "") or "").strip()
    if legacy_path:
        return f"{setting_name} or GOOGLE_APPLICATION_CREDENTIALS", legacy_path

    raise RuntimeError(
        f"No Google credentials file configured for {service}. "
        f"Set {setting_name}."
    )


def resolve_google_credentials_path(service: GoogleService) -> Path:
    """Resolve and validate the configured credential file path for a service."""
    source_name, configured_path = _credentials_source(service)
    resolved_path = _resolve_relative_path(configured_path)
    if not resolved_path.exists():
        raise FileNotFoundError(
            f"{source_name} points to a missing file: '{resolved_path}'."
        )
    return resolved_path


def get_gee_project_id() -> str:
    """Return the configured Earth Engine project id."""
    project_id = str(getattr(settings, "GEE_PROJECT_ID", "") or "").strip()
    if project_id:
        return project_id

    legacy_project_id = str(getattr(settings, "GOOGLE_CLOUD_PROJECT", "") or "").strip()
    if legacy_project_id:
        return legacy_project_id

    raise RuntimeError(
        "GEE_PROJECT_ID is not configured. Set GEE_PROJECT_ID "
        "or legacy GOOGLE_CLOUD_PROJECT."
    )