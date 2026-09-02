"""Redis-backed request rate limiting helpers aligned with the backend spec."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException, status
from redis import Redis

from app.config import settings
from app.redis_utils import redis_from_url

logger = logging.getLogger("terratrust.rate_limit")


@dataclass(frozen=True)
class RateLimitSpec:
    """Configuration for a single per-user rate-limited scope."""

    scope: str
    limit: int
    window_seconds: int
    error_message: str = "Rate limit exceeded. Please try again later."


_redis_client: Redis | None = None
_redis_unavailable_until = 0.0
_memory_lock = threading.Lock()
_memory_counters: dict[str, tuple[int, float]] = {}
_REDIS_FAILURE_COOLDOWN_SECONDS = 60
_RATE_LIMIT_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
  return { current, tonumber(ARGV[1]) }
end

local ttl = redis.call('TTL', KEYS[1])
if ttl < 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
  ttl = tonumber(ARGV[1])
end

return { current, ttl }
"""


def _get_redis_client() -> Redis | None:
    """Return a cached Redis client without issuing an eager network command."""
    global _redis_client

    if _redis_unavailable_until > time.time():
        return None

    if _redis_client is not None:
        return _redis_client

    try:
        _redis_client = redis_from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    except Exception as exc:
        logger.warning("Redis unavailable for rate limiting: %s", exc)
        _redis_client = None

    return _redis_client


def _mark_redis_unavailable(exc: Exception) -> None:
    """Temporarily stop trying Redis after a command failure."""
    global _redis_client, _redis_unavailable_until

    logger.warning(
        "Redis rate-limit backend failed; using memory fallback for %ds: %s",
        _REDIS_FAILURE_COOLDOWN_SECONDS,
        exc,
    )
    _redis_client = None
    _redis_unavailable_until = time.time() + _REDIS_FAILURE_COOLDOWN_SECONDS


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

    result = redis_client.eval(_RATE_LIMIT_SCRIPT, 1, key, int(window_seconds))
    count, ttl = int(result[0]), int(result[1])
    return count, max(1, ttl)


def enforce_rate_limit(user_id: str, spec: RateLimitSpec) -> None:
    """Raise HTTP 429 when a per-user quota is exceeded."""
    cache_key = f"rate-limit:{spec.scope}:{user_id}"

    try:
        count, retry_after = _consume_redis_window(cache_key, spec.window_seconds)
    except Exception as exc:
        _mark_redis_unavailable(exc)
        count, retry_after = _consume_memory_window(cache_key, spec.window_seconds)

    if count <= spec.limit:
        return

    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=spec.error_message,
        headers={"Retry-After": str(retry_after)},
    )
