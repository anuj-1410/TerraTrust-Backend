"""Validate declared institution configuration without accessing any service."""
import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


def check(config, *, advisor=False):
    errors = {name: "REQUIRED" for name in config.institution_missing_config(advisor=advisor)}
    for name in ("INSTITUTION_PRIVACY_POLICY_URL", "INSTITUTION_SUPPORT_URL", "INSTITUTION_ACCOUNT_DELETION_URL"):
        try:
            url = urlsplit(getattr(config, name))
            valid = url.scheme == "https" and bool(url.hostname) and not url.username and not url.password and url.port in {None, 443}
        except ValueError:
            valid = False
        if not valid:
            errors[name] = "PUBLIC_HTTPS_URL_REQUIRED"
    from services.shared.receipt_crypto_service import local_key
    try:
        local_key()
    except Exception:
        errors[".institution-private/receipt.key"] = "RUN_CREATE_RECEIPT_KEY_ONCE"
    path = Path(config.INSTITUTION_REPORT_FONT_PATH)
    if not config.INSTITUTION_REPORT_FONT_PATH or not path.is_file():
        errors["INSTITUTION_REPORT_FONT_PATH"] = "LICENSED_UNICODE_TTF_REQUIRED"
    else:
        try:
            if path.stat().st_size > 20 * 1024 * 1024 or hashlib.sha256(path.read_bytes()).hexdigest() != config.INSTITUTION_REPORT_FONT_SHA256:
                errors["INSTITUTION_REPORT_FONT_SHA256"] = "FONT_CHECKSUM_MISMATCH"
        except OSError:
            errors["INSTITUTION_REPORT_FONT_PATH"] = "FONT_UNREADABLE"
    if config.DATABASE_SSL_CA_FILE:
        try:
            from app.database_ssl import database_ssl_context
            database_ssl_context(config.DATABASE_SSL_CA_FILE)
        except (OSError, ValueError, RuntimeError):
            errors["DATABASE_SSL_CA_FILE"] = "TRUSTED_CA_PEM_REQUIRED"
    if advisor:
        for name in ("LLM_PROVIDER", "EMBEDDING_PROVIDER"):
            if getattr(config, name) != "GEMINI":
                errors[name] = "GEMINI_REQUIRED"
        for name in ("LLM_MODEL", "EMBEDDING_MODEL"):
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", getattr(config, name)):
                errors[name] = "EXPLICIT_GEMINI_MODEL_ID_REQUIRED"
        if not 1 <= config.EMBEDDING_DIMENSION <= 3072:
            errors["EMBEDDING_DIMENSION"] = "REVIEWED_DIMENSION_REQUIRED"
    return [{"setting": name, "code": code} for name, code in sorted(errors.items())]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--advisor", action="store_true")
    args = parser.parse_args()
    try:
        from app.config import settings
        errors = check(settings, advisor=args.advisor)
    except Exception as exc:
        # Config exceptions may contain secrets; disclose only safe field paths.
        from pydantic import ValidationError
        errors = [{"setting": ".".join(map(str, error["loc"])), "code": "INVALID_CONFIGURATION"} for error in exc.errors(include_input=False, include_context=False)] if isinstance(exc, ValidationError) else [{"setting": "configuration", "code": "CONFIGURATION_UNAVAILABLE"}]
    print(json.dumps({"ok": not errors, "mode": "OFFLINE_CONFIGURATION_ONLY", "errors": errors}, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
