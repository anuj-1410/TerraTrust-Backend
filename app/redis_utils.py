"""Shared Redis URL helpers for runtime services."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

if TYPE_CHECKING:
    from redis import Redis
else:
    Redis = Any

_SSL_CERT_REQS_ALIASES = {
    "CERT_NONE": "none",
    "CERT_OPTIONAL": "optional",
    "CERT_REQUIRED": "required",
}


def normalise_redis_url(redis_url: str) -> str:
    """Map legacy SSL requirement flags to the values accepted by redis-py."""
    if not redis_url:
        return redis_url

    parts = urlsplit(redis_url)
    if not parts.query:
        return redis_url

    changed = False
    query_pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "ssl_cert_reqs":
            mapped_value = _SSL_CERT_REQS_ALIASES.get(value.upper())
            if mapped_value:
                value = mapped_value
                changed = True
        query_pairs.append((key, value))

    if not changed:
        return redis_url

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query_pairs, doseq=True),
            parts.fragment,
        )
    )


def secure_redis_url(redis_url: str) -> str:
    """Force TLS-enabled Redis URLs to validate certificates by default."""
    if not redis_url:
        return redis_url

    normalised_url = normalise_redis_url(redis_url)
    parts = urlsplit(normalised_url)
    if parts.scheme != "rediss":
        return normalised_url

    changed = False
    saw_ssl_cert_reqs = False
    query_pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "ssl_cert_reqs":
            saw_ssl_cert_reqs = True
            if value != "required":
                value = "required"
                changed = True
        query_pairs.append((key, value))

    if not saw_ssl_cert_reqs:
        query_pairs.append(("ssl_cert_reqs", "required"))
        changed = True

    if not changed:
        return normalised_url

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query_pairs, doseq=True),
            parts.fragment,
        )
    )


def redis_from_url(redis_url: str, **kwargs) -> Redis:
    """Create a Redis client after normalising URL compatibility quirks."""
    from redis import Redis

    return Redis.from_url(normalise_redis_url(redis_url), **kwargs)
