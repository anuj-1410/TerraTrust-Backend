"""ASGI middleware that restores resettable demo accounts on the next login."""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.firebase_auth import verify_firebase_token
from demo.config import ENABLE_DEMO_ACCOUNTS, is_resettable_demo
from demo.restore import restore_to_checkpoint

logger = logging.getLogger("terratrust.demo.middleware")

# Only 3 UIDs can ever enter this set (accounts 1-3).
_reset_done_this_session: set[str] = set()


class DemoResetMiddleware(BaseHTTPMiddleware):
    """Reset a demo account before ``GET /api/v1/auth/me`` reads its state."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if not ENABLE_DEMO_ACCOUNTS:
            return await call_next(request)

        if request.url.path != "/api/v1/auth/me" or request.method != "GET":
            return await call_next(request)

        firebase_uid = await _extract_firebase_uid(request)
        if not firebase_uid:
            return await call_next(request)

        if not is_resettable_demo(firebase_uid):
            return await call_next(request)

        if firebase_uid in _reset_done_this_session:
            return await call_next(request)

        try:
            restored = await restore_to_checkpoint(firebase_uid)
        except Exception as exc:
            logger.warning("[DEMO] Middleware restore error for %s: %s", firebase_uid, exc)
            restored = False

        if restored:
            _reset_done_this_session.add(firebase_uid)

        return await call_next(request)


async def _extract_firebase_uid(request: Request) -> str | None:
    """Return the Firebase UID from the bearer token, or ``None`` on any error."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None

    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        return None

    try:
        decoded = verify_firebase_token(token)
    except Exception:
        return None

    firebase_uid = decoded.get("uid")
    return str(firebase_uid) if firebase_uid else None


def invalidate_demo_session(firebase_uid: str) -> None:
    """Allow the next ``/auth/me`` call to trigger a new demo reset."""
    _reset_done_this_session.discard(firebase_uid)
