import sys
import types

import pytest

fastapi_stub = types.ModuleType("fastapi")


class _HTTPException(Exception):
    def __init__(self, status_code, detail, headers=None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.headers = headers or {}


fastapi_stub.HTTPException = _HTTPException
fastapi_stub.status = types.SimpleNamespace(HTTP_429_TOO_MANY_REQUESTS=429)
sys.modules["fastapi"] = fastapi_stub

redis_stub = types.ModuleType("redis")


class _RedisStub:
    @classmethod
    def from_url(cls, *_args, **_kwargs):
        raise RuntimeError("redis unavailable in unit-test stub")


redis_stub.Redis = _RedisStub
sys.modules.setdefault("redis", redis_stub)

sys.modules.pop("app.rate_limit", None)

from app import rate_limit


def test_enforce_rate_limit_uses_memory_fallback(monkeypatch):
    monkeypatch.setattr(rate_limit, "_get_redis_client", lambda: None)
    monkeypatch.setattr(rate_limit, "_memory_counters", {})
    monkeypatch.setattr(rate_limit, "_redis_unavailable_until", 0.0)

    spec = rate_limit.RateLimitSpec(
        scope="audit.result",
        limit=2,
        window_seconds=60,
        error_message="Too many result polls.",
    )

    rate_limit.enforce_rate_limit("user-1", spec)
    rate_limit.enforce_rate_limit("user-1", spec)

    with pytest.raises(Exception) as exc_info:
        rate_limit.enforce_rate_limit("user-1", spec)

    assert exc_info.value.status_code == 429
    assert exc_info.value.detail == "Too many result polls."
    assert int(exc_info.value.headers["Retry-After"]) > 0


def test_enforce_rate_limit_resets_after_window(monkeypatch):
    class _Clock:
        def __init__(self):
            self.current = 1_000.0

        def time(self):
            return self.current

    clock = _Clock()

    monkeypatch.setattr(rate_limit, "_get_redis_client", lambda: None)
    monkeypatch.setattr(rate_limit, "_memory_counters", {})
    monkeypatch.setattr(rate_limit, "_redis_unavailable_until", 0.0)
    monkeypatch.setattr(rate_limit.time, "time", clock.time)

    spec = rate_limit.RateLimitSpec(
        scope="credits.balance",
        limit=1,
        window_seconds=10,
    )

    rate_limit.enforce_rate_limit("user-1", spec)

    with pytest.raises(Exception) as exc_info:
        rate_limit.enforce_rate_limit("user-1", spec)

    assert exc_info.value.status_code == 429

    clock.current += 11
    rate_limit.enforce_rate_limit("user-1", spec)


def test_redis_client_is_created_without_eager_ping(monkeypatch):
    class _RedisClient:
        def __init__(self):
            self.ping_called = False

        def ping(self):
            self.ping_called = True
            raise AssertionError("rate limiter should not ping Redis eagerly")

    client = _RedisClient()

    monkeypatch.setattr(rate_limit, "_redis_client", None)
    monkeypatch.setattr(rate_limit, "_redis_unavailable_until", 0.0)
    monkeypatch.setattr(
        rate_limit,
        "settings",
        types.SimpleNamespace(REDIS_URL="redis://localhost:6379/0"),
    )
    monkeypatch.setattr(rate_limit, "redis_from_url", lambda *_args, **_kwargs: client)

    assert rate_limit._get_redis_client() is client
    assert client.ping_called is False


def test_redis_rate_limit_uses_single_eval_command(monkeypatch):
    class _RedisClient:
        def __init__(self):
            self.eval_calls = []

        def eval(self, script, numkeys, key, window_seconds):
            self.eval_calls.append((script, numkeys, key, window_seconds))
            return [1, window_seconds]

    client = _RedisClient()

    monkeypatch.setattr(rate_limit, "_redis_client", client)
    monkeypatch.setattr(rate_limit, "_redis_unavailable_until", 0.0)

    count, retry_after = rate_limit._consume_redis_window("rate-limit:test:user-1", 60)

    assert count == 1
    assert retry_after == 60
    assert len(client.eval_calls) == 1
