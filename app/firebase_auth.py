"""Firebase Admin initialisation and ID-token verification helpers."""

from __future__ import annotations

import logging
from typing import Any, Dict

import firebase_admin
from firebase_admin import auth, credentials

from app.config import settings
from app.google_credentials import resolve_google_credentials_path as resolve_service_credentials_path

logger = logging.getLogger("terratrust.firebase_auth")


def resolve_google_credentials_path():
    """Resolve the Firebase Admin credentials file relative to the backend root."""
    return resolve_service_credentials_path("firebase")


def _ensure_google_credentials_env():
    """Validate and return the Firebase Admin credential file path."""
    return resolve_google_credentials_path()


def get_firebase_app() -> firebase_admin.App:
    """Return the shared Firebase Admin app, initialising it on first use."""
    try:
        return firebase_admin.get_app()
    except ValueError:
        pass

    credentials_path = _ensure_google_credentials_env()
    app = firebase_admin.initialize_app(
        credentials.Certificate(str(credentials_path)),
        {"projectId": settings.FIREBASE_PROJECT_ID},
    )
    logger.info("Firebase Admin initialised using credentials from %s", credentials_path)
    return app


def verify_firebase_token(id_token: str) -> Dict[str, Any]:
    """Verify a Firebase ID token and return the decoded claims."""
    return auth.verify_id_token(id_token, app=get_firebase_app())