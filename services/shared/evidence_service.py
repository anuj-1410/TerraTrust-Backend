"""Bounded private storage, byte validation and immutable promotion."""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
from pathlib import PurePath

from PIL import Image
from pypdf import PdfReader

from app.config import settings
from app.institution_errors import fail


async def read_bytes(bucket, key):
    from app.database import supabase_client
    try:
        data = await asyncio.to_thread(supabase_client.storage.from_(bucket).download, key)
    except Exception:
        fail(503, "STORAGE_UNAVAILABLE", "Private source bytes are temporarily unavailable.", retryable=True)
    if len(data) > settings.INSTITUTION_DOWNLOAD_MAX_BYTES:
        fail(413, "FILE_TOO_LARGE", "Private object exceeds the supported byte limit.")
    return data


async def write_bytes(bucket, key, data, mime):
    """Content-addressed writes reconcile a lost upload response by exact bytes."""
    from app.database import supabase_client
    try:
        await asyncio.to_thread(supabase_client.storage.from_(bucket).upload, key, data, {"content-type": mime, "upsert": "false"})
    except Exception:
        existing = await read_bytes(bucket, key)
        if existing != data:
            fail(503, "STORAGE_WRITE_CONFLICT", "Immutable object storage could not be verified.")
    actual = await read_bytes(bucket, key)
    if len(actual) != len(data) or hashlib.sha256(actual).digest() != hashlib.sha256(data).digest():
        fail(503, "STORAGE_INTEGRITY_FAILURE", "Written private bytes could not be verified.")


def inspect_bytes(data, mime, filename, *, model=False):
    extension = PurePath(filename).suffix.lower()
    if mime == "application/pdf" and extension == ".pdf":
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]:
            raise ValueError("Invalid PDF byte signature.")
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted or len(reader.pages) > 1000:
            raise ValueError("Encrypted or excessively large PDF is unsupported.")
        root = reader.trailer["/Root"]
        if any(key in root for key in ("/OpenAction", "/AA")) or any(marker in data for marker in (b"/JavaScript", b"/JS", b"/Launch", b"/EmbeddedFiles")):
            raise ValueError("Active or embedded PDF content is unsupported.")
        for page in reader.pages:
            _ = page.mediabox
    elif mime in {"image/jpeg", "image/png"} and extension in {".jpg", ".jpeg", ".png"}:
        if mime == "image/png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Invalid PNG byte signature.")
        if mime == "image/jpeg" and (not data.startswith(b"\xff\xd8\xff") or not data.endswith(b"\xff\xd9")):
            raise ValueError("Invalid JPEG byte signature.")
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"JPEG", "PNG"} or image.width * image.height > 40_000_000:
                raise ValueError("Unsupported image dimensions/format.")
            image.verify()
    elif mime == "text/csv" and extension == ".csv":
        decoded = data.decode("utf-8-sig")
        if "\x00" in decoded:
            raise ValueError("CSV contains binary data.")
        reader = csv.reader(io.StringIO(decoded, newline=""), strict=True)
        for index, row in enumerate(reader):
            if index > 10000:
                raise ValueError("CSV exceeds 10000 data rows.")
    elif model and mime == "application/octet-stream" and extension in {".json", ".ubj", ".tflite"}:
        if extension == ".json":
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError("Model descriptor must be an object.")
        elif extension == ".tflite":
            if data[4:8] != b"TFL3":
                raise ValueError("Invalid TFLite FlatBuffer signature.")
        elif not data.startswith(b"{"):
            raise ValueError("Invalid UBJSON model signature.")
    else:
        raise ValueError("MIME type and file extension are incompatible.")


async def verify_and_promote(row):
    data = await read_bytes(row["private_bucket"], row["object_key"])
    if len(data) != row["size_bytes"] or hashlib.sha256(data).hexdigest() != row["expected_sha256"]:
        fail(422, "FILE_HASH_MISMATCH", "Actual byte size/hash differs from the selected evidence.")
    try:
        await asyncio.wait_for(asyncio.to_thread(inspect_bytes, data, row["mime_type"], row["file_name"], model=row["parent_type"] == "MODEL"), timeout=30)
    except ValueError:
        fail(422, "INVALID_FILE", "Source bytes failed file-format validation.")
    key = f"verified/{row['id']}/{row['expected_sha256']}"
    await write_bytes(row["private_bucket"], key, data, row["mime_type"])
    return {"object_key": key, "byte_hash": row["expected_sha256"], "actual_size_bytes": len(data), "state": "READY"}


async def download(row, *, artifact=False):
    from datetime import datetime, timedelta, timezone
    from app.database import supabase_client
    if row.get("bytes_deleted_at"):
        fail(410, "RESOURCE_BYTES_PURGED", "These private bytes were deleted under the approved retention policy.")
    if row["status" if artifact else "state"] != "READY":
        fail(409, "FILE_NOT_READY", "This file is not ready for download.")
    ttl = settings.INSTITUTION_SIGNED_DOWNLOAD_TTL_SECONDS
    try:
        result = await asyncio.to_thread(supabase_client.storage.from_(row["private_bucket"]).create_signed_url, row["object_key"], ttl)
        url = result.get("signedURL") or result.get("signedUrl") or result.get("signed_url")
        if not url:
            raise ValueError()
    except Exception:
        fail(503, "STORAGE_UNAVAILABLE", "Private download is temporarily unavailable.", retryable=True)
    return {"url": url, "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat().replace("+00:00", "Z"), "file_name": row.get("file_name", "terratrust-report"), "mime_type": row["mime_type"], "size_bytes": row["size_bytes"] if artifact else row["actual_size_bytes"], "sha256": row["byte_hash"]}
