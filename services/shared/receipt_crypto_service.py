"""Local AES-256-GCM for scoped, expiring invitation receipts."""

import base64
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.institution_errors import fail
from models.institution.common import canonical, normalize

KEY_FILE = Path(__file__).resolve().parents[2] / ".institution-private" / "receipt.key"
MAGIC = b"TTR1"


def local_key():
    try:
        encoded = KEY_FILE.read_bytes().strip()
        value = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if len(value) != 32:
            raise ValueError()
        return value
    except (OSError, ValueError):
        fail(
            503,
            "RECEIPT_KEY_UNAVAILABLE",
            "Create the local invitation key with scripts.institution.create_receipt_key.",
        )


def key_id(key):
    return "local-aes256gcm:" + hashlib.sha256(key).hexdigest()[:24]


def aad(scope, request_hash, expires):
    return canonical(
        {
            "schema_version": 1,
            "scope": scope,
            "request_hash": request_hash,
            "secret_expires_at": normalize(expires),
        }
    )


async def encrypt_receipt(token, scope, request_hash, expires):
    if expires <= datetime.now(timezone.utc):
        fail(
            409,
            "IDEMPOTENCY_RECEIPT_EXPIRED",
            "Invitation receipt expiry must be in the future.",
        )
    key, nonce = local_key(), os.urandom(12)
    ciphertext = (
        MAGIC
        + nonce
        + AESGCM(key).encrypt(
            nonce, token.encode("utf-8"), aad(scope, request_hash, expires)
        )
    )
    return {
        "secret_ciphertext": ciphertext,
        "encryption_key_id": key_id(key),
        "secret_expires_at": expires,
    }


async def decrypt_receipt(receipt, scope, request_hash):
    if receipt["secret_expires_at"] <= datetime.now(timezone.utc):
        fail(
            409,
            "IDEMPOTENCY_RECEIPT_EXPIRED",
            "This invitation receipt expired; explicitly reissue the code.",
        )
    key = local_key()
    if receipt["encryption_key_id"] != key_id(key):
        fail(
            409,
            "RECEIPT_KEY_CHANGED",
            "Restore the original local key or explicitly reissue this invitation.",
        )
    raw = bytes(receipt["secret_ciphertext"])
    try:
        if len(raw) < 32 or raw[:4] != MAGIC:
            raise ValueError()
        return (
            AESGCM(key)
            .decrypt(
                raw[4:16],
                raw[16:],
                aad(scope, request_hash, receipt["secret_expires_at"]),
            )
            .decode("utf-8")
        )
    except (InvalidTag, ValueError, UnicodeError):
        fail(
            409,
            "RECEIPT_AUTHENTICATION_FAILED",
            "The invitation receipt could not be authenticated.",
        )
