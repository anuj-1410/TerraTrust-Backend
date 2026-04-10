"""Shared Google Earth Engine initialization helpers."""

from __future__ import annotations

import json
import logging

import ee

from app.google_credentials import get_gee_project_id, resolve_google_credentials_path as resolve_service_credentials_path

logger = logging.getLogger("terratrust.gee")


def resolve_google_credentials_path():
    """Resolve the Earth Engine credentials file relative to the backend root."""
    return resolve_service_credentials_path("gee")


def _build_gee_credentials(credentials_path):
    """Create Earth Engine service-account credentials from the configured file."""
    with credentials_path.open("r", encoding="utf-8") as handle:
        service_account_info = json.load(handle)

    client_email = str(service_account_info.get("client_email") or "").strip()
    if not client_email:
        raise RuntimeError(
            f"GEE credentials file is missing client_email: '{credentials_path}'."
        )

    return ee.ServiceAccountCredentials(client_email, str(credentials_path))


def has_gee_configuration() -> bool:
    """Return whether the dedicated Earth Engine configuration is available."""
    try:
        resolve_google_credentials_path()
        get_gee_project_id()
    except (FileNotFoundError, RuntimeError):
        return False
    return True


def ensure_gee_initialized() -> None:
    """Initialise Google Earth Engine once using the configured service account."""
    try:
        ee.Number(1).getInfo()
        return
    except Exception:
        pass

    credentials_path = resolve_google_credentials_path()
    project_id = get_gee_project_id()
    ee.Initialize(credentials=_build_gee_credentials(credentials_path), project=project_id)
    logger.info(
        "Google Earth Engine initialised for project %s using credentials from %s.",
        project_id,
        credentials_path,
    )