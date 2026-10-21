"""Farmer-style demo resets: private checkpoints, in-process login markers, normal DB transactions."""

import asyncio
import json
from uuid import NAMESPACE_URL, uuid5, uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.institution_access import identity, recent_auth
from app.institution_errors import fail
from demo.institution.registry import require_demo, account_for
from demo.institution.blueprints import phone_for, reset_policy
from models.institution.common import normalize, uuid_text
from repositories.institution.base import Repository

router = APIRouter(tags=["Institution demo"])


# Like demo.middleware, demo-session tracking lives in the API process.
# Institution demos are local/development only; use one API process.
_login_states = {}
_login_locks = {}


async def session_state(uid, value=None):
    if value is not None:
        _login_states[uid] = dict(value)
    state = _login_states.get(uid)
    return dict(state) if state else None


def invalidate_demo_session(uid):
    _login_states.pop(uid, None)


async def current_state(uid, phone):
    """Normal identities never touch the demo session map or need a demo header."""
    if not settings.ENABLE_INSTITUTION_DEMO_ACCOUNTS:
        return None
    require_demo()
    source = account_for(uid, phone)
    if source is None:
        return None
    state = await session_state(uid)
    if not state or state["status"] != "READY":
        fail(
            409,
            "DEMO_SESSION_NOT_READY",
            "Sign in to initialize the demo checkpoint.",
            retryable=True,
        )
    return state


def resource(source, auth_time, *, restored=False):
    return {
        "session_id": str(
            uuid5(
                NAMESPACE_URL,
                f"tt:institution-demo:{source.target_firebase_uid}:{auth_time}",
            )
        ),
        "status": "READY",
        "account_number": source.account_number,
        "account_domain": "INSTITUTION",
        "checkpoint_key": source.checkpoint_key,
        "reset_performed": restored,
        "fixture_generation": 1 if source.account_number == 9 else auth_time,
        "reset_policy": reset_policy(source.account_number),
        "audit_demo_mode": True,
        "poll_after_seconds": None,
        "safe_error": None,
        "auth_time": auth_time,
    }


def response(value):
    data = {key: item for key, item in value.items() if key != "auth_time"}
    return JSONResponse(
        {
            "data": normalize(data),
            "request_id": str(uuid4()),
            "revision": None,
            "context_id": None,
            "warnings": [],
            "fixture_generation": value["fixture_generation"],
        },
        headers={
            "Cache-Control": "private, no-store",
            "X-TerraTrust-Demo-Generation": str(value["fixture_generation"]),
        },
    )


async def ensure_login(claims):
    """auth_time remains unchanged on token refresh, so ongoing work is preserved."""
    require_demo()
    source = account_for(claims["uid"], claims["phone_number"])
    if source is None:
        return None
    if claims["phone_number"] != phone_for(source.account_number):
        fail(
            409,
            "ACCOUNT_DOMAIN_MISMATCH",
            "The verified demo identity does not match its configured phone.",
        )
    auth_time = claims.get("auth_time")
    if type(auth_time) is not int or auth_time <= 0:
        fail(
            401,
            "RECENT_AUTH_REQUIRED",
            "Sign in again using Firebase phone authentication.",
        )
    from app.institution_database import _require_async_engine

    # Serialize the five demo identities in memory, as farmer middleware does.
    # Row locking and checkpoint replay use the existing database transaction.
    lock = _login_locks.setdefault(claims["uid"], asyncio.Lock())
    async with lock:
        previous = await session_state(claims["uid"])
        if previous and previous["auth_time"] > auth_time:
            fail(409, "DEMO_SESSION_RESET", "A newer sign-in already exists.")
        if (
            previous
            and previous["auth_time"] == auth_time
            and previous["status"] == "READY"
        ):
            return previous
        recent_auth(claims)
        value = resource(source, auth_time)
        await session_state(claims["uid"], {**value, "status": "RESTORING"})
        async with _require_async_engine().begin() as conn:
            repo = Repository(conn)
            user = await repo.one(
                "SELECT * FROM users WHERE firebase_uid=:uid FOR UPDATE",
                {"uid": claims["uid"]},
            )
            if (
                user["account_domain"] != "INSTITUTION"
                or str(user["id"]) != source.target_user_id
                or user["phone_number"] != claims["phone_number"]
            ):
                fail(
                    409,
                    "ACCOUNT_DOMAIN_MISMATCH",
                    "The verified checkpoint identity/domain differs.",
                )
            if source.account_number != 9:
                from demo.institution.restore import restore_to_checkpoint

                await restore_to_checkpoint(repo, source, value["fixture_generation"])
                value["reset_performed"] = True
        await session_state(claims["uid"], value)
        return value


async def start_session(claims, body):
    require_demo()
    source = account_for(claims["uid"], claims["phone_number"])
    if source is None:
        fail(
            404,
            "RESOURCE_NOT_FOUND",
            "This identity has no configured demo checkpoint.",
        )
    if body.get("checkpoint_key") not in {None, source.checkpoint_key}:
        fail(
            409,
            "DEMO_CHECKPOINT_MISMATCH",
            "The selected checkpoint differs from this identity.",
        )
    return response(await ensure_login(claims))


@router.post("/sessions")
async def start(request: Request):
    require_demo()
    claims = await identity(request.headers.get("Authorization"))
    raw = await request.body()
    if len(raw) > 4096:
        fail(413, "REQUEST_TOO_LARGE", "Session request exceeds its supported size.")
    try:
        from app.institution_context import unique_object

        body = json.loads(raw, object_pairs_hook=unique_object)
        if set(body) - {
            "login_session_id",
            "flow",
            "client_audit_demo_mode",
            "checkpoint_key",
        }:
            raise ValueError()
        uuid_text(body["login_session_id"])
    except (ValueError, KeyError, TypeError):
        fail(422, "INVALID_DEMO_SESSION", "Supply the typed login-session request.")
    if request.headers.get("Idempotency-Key") != body["login_session_id"]:
        fail(
            400,
            "IDEMPOTENCY_KEY_REQUIRED",
            "Session Idempotency-Key must equal login_session_id.",
        )
    if (
        body.get("flow") != "INSTITUTION"
        or body.get("client_audit_demo_mode") is not True
    ):
        fail(
            409,
            "DEMO_CONFIGURATION_MISMATCH",
            "Institution demo flow and shared mode must agree.",
        )
    return await start_session(claims, body)


@router.get("/sessions/{session_id}")
async def status(session_id: str, request: Request):
    require_demo()
    claims = await identity(request.headers.get("Authorization"))
    state = await current_state(claims["uid"], claims["phone_number"])
    if not state or state["session_id"] != session_id:
        fail(404, "RESOURCE_NOT_FOUND", "This demo sign-in is unavailable.")
    return response(state)
