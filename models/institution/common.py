"""Strict wire primitives and the MCJ1/SCJ1 canonicalization contract."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext, ROUND_HALF_EVEN
from typing import Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, BeforeValidator, ConfigDict, model_validator

DECIMAL_PATTERN = r"^-?(0|[1-9][0-9]{0,11})(\.[0-9]{1,12})?$"
QUANTUM = Decimal("0.000000000001")
PI = Decimal("3.1415926535897932384626433832795028841971693993751")


def decimal_text(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(DECIMAL_PATTERN, value):
        raise ValueError("Expected a decimal string fitting NUMERIC(24,12).")
    return value


def uuid_text(value: Any) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Expected a canonical lowercase UUID.")
    return value


def sha256_text(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Expected a lowercase SHA-256 digest.")
    return value


def date_text(value: Any) -> str:
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Expected YYYY-MM-DD.")
    return value


def timestamp_text(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)", value):
        raise ValueError("Expected an RFC3339 UTC timestamp.")
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


DecimalText = Annotated[str, BeforeValidator(decimal_text)]
UUIDText = Annotated[str, BeforeValidator(uuid_text)]
HashText = Annotated[str, BeforeValidator(sha256_text)]
DateText = Annotated[str, BeforeValidator(date_text)]
TimestampText = Annotated[str, BeforeValidator(timestamp_text)]


def numeric(value: Decimal) -> str:
    """Quantize only derived output, never a client input."""
    with localcontext() as ctx:
        ctx.prec = 50
        result = value.quantize(QUANTUM, rounding=ROUND_HALF_EVEN)
    if not result.is_finite() or abs(result) >= Decimal("1e12"):
        raise ValueError("NUMERIC(24,12) overflow.")
    return format(abs(result) if result == 0 else result, ".12f")


def normalize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return numeric(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("Naive timestamp.")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, (date, UUID)):
        return str(value)
    if isinstance(value, BaseModel):
        return normalize(value.model_dump(exclude_unset=True))
    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(v) for v in value]
    return value


def canonical(value: Any) -> bytes:
    """ECMAScript-compatible JSON primitives, UTF-16 key order (MCJ1)."""
    import rfc8785
    def binary64(item):
        if isinstance(item, int) and not isinstance(item, bool):
            if not math.isfinite(float(item)) or int(float(item)) != item:
                raise ValueError("Integer is not exactly representable as binary64.")
            return float(item) if abs(item) > 9007199254740991 else item
        if isinstance(item, list):
            return [binary64(child) for child in item]
        if isinstance(item, dict):
            return {key: binary64(child) for key, child in item.items()}
        return item
    return rfc8785.dumps(binary64(value))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(normalize(value))).hexdigest()


def measurement_hash(raw: dict) -> str:
    return hashlib.sha256(canonical({k: v for k, v in raw.items() if k != "client_content_hash"})).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, defer_build=True)

    @model_validator(mode="after")
    def common_constraints(self):
        values = self.model_dump(exclude_unset=True)
        for key, value in values.items():
            if isinstance(value, str) and len(value) > (2048 if key.endswith("url") else 2000):
                raise ValueError(f"{key} exceeds its text limit.")
            if isinstance(value, list) and len(value) > 200 and key not in {"rows", "row_hashes", "objects"}:
                raise ValueError(f"{key} exceeds 200 items.")
            if key in {"timezone", "reporting_timezone"} and value is not None:
                try:
                    ZoneInfo(value)
                except ZoneInfoNotFoundError as exc:
                    raise ValueError("Unknown IANA timezone.") from exc
            if key.endswith("phone_e164") and value is not None and not re.fullmatch(r"\+[1-9][0-9]{7,14}", value):
                raise ValueError("Expected E.164 phone number.")
        for prefix in ("period", "activity", "survey", "observation", "effective", "comparison"):
            start, end = values.get(prefix + "_start"), values.get(prefix + "_end")
            if start is not None and end is not None and end < start:
                raise ValueError(f"{prefix}_end precedes {prefix}_start.")
        return self
