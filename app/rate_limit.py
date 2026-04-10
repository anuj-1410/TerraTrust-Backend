"""Redis-backed request rate limiting helpers aligned with the backend spec."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException, status
from redis import Redis

from app.config import settings

logger = logging.getLogger("terratrust.rate_limit")


@dataclass(frozen=True)
class RateLimitSpec:
    """Configuration for a single per-user rate-limited scope."""

    scope: str
    limit: int
    window_seconds: int
    error_message: str = "Rate limit exceeded. Please try again later."


_redis_client: Redis | None = None
_redis_initialised = False
_memory_lock = threading.Lock()
_memory_counters: dict[str, tuple[int, float]] = {}


def _get_redis_client() -> Redis | None:
    """Return a cached Redis client, falling back to in-memory counters when unavailable."""
    global _redis_client, _redis_initialised

    if _redis_initialised:
        return _redis_client

    _redis_initialised = True
    try:
        _redis_client = Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        _redis_client.ping()
    except Exception as exc:
        logger.warning("Redis unavailable for rate limiting: %s", exc)
        _redis_client = None

    return _redis_client


def _consume_memory_window(key: str, window_seconds: int) -> tuple[int, int]:
    """Consume one request from an in-memory sliding window fallback."""
    now = time.time()

    with _memory_lock:
        count, reset_at = _memory_counters.get(key, (0, now + window_seconds))
        if now >= reset_at:
            count = 0
            reset_at = now + window_seconds

        count += 1
        _memory_counters[key] = (count, reset_at)

    retry_after = max(1, int(reset_at - now))
    return count, retry_after


def _consume_redis_window(key: str, window_seconds: int) -> tuple[int, int]:
    """Consume one request from a Redis-backed fixed window."""
    redis_client = _get_redis_client()
    if redis_client is None:
        return _consume_memory_window(key, window_seconds)

    count = int(redis_client.incr(key))
    if count == 1:
        redis_client.expire(key, window_seconds)
        return count, window_seconds

    ttl = int(redis_client.ttl(key))
    if ttl < 0:
        redis_client.expire(key, window_seconds)
        ttl = window_seconds

    return count, max(1, ttl)


def enforce_rate_limit(user_id: str, spec: RateLimitSpec) -> None:
    """Raise HTTP 429 when a per-user quota is exceeded."""
    cache_key = f"rate-limit:{spec.scope}:{user_id}"

    try:
        count, retry_after = _consume_redis_window(cache_key, spec.window_seconds)
    except Exception as exc:
        logger.warning(
            "Rate-limit counter backend failed for scope %s and user %s: %s",
            spec.scope,
            user_id,
            exc,
        )
        count, retry_after = _consume_memory_window(cache_key, spec.window_seconds)

    if count <= spec.limit:
        return

    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=spec.error_message,
        headers={"Retry-After": str(retry_after)},
    )