"""Private local checkpoints, like farmer code checkpoints; no demo DB tables."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.institution_errors import fail
from demo.institution.manifest import CheckpointManifest, UserCheckpoint
from demo.institution.blueprints import BLUEPRINTS, phone_for, reset_policy

DIRECTORY = Path(__file__).resolve().parents[2] / ".institution-private" / "demo"
SECRET_DIRECTORY = Path("/etc/secrets")


@dataclass(frozen=True)
class FreshCheckpoint:
    """Account five has no approvals/results to seed: its real baseline is empty."""

    target_firebase_uid: str
    target_user_id: str | None = None
    account_number: int = 5
    checkpoint_key: str = "INSTITUTION_FRESH"
    owned_tenant_ids: tuple = ()
    tables: tuple = ()
    objects: tuple = ()
    user_checkpoint: UserCheckpoint = field(
        default_factory=lambda: UserCheckpoint(
            onboarding_status="REQUIRED",
            onboarding_completed_at=None,
            registration_id=None,
            registration_institution_id=None,
        )
    )


def checkpoint_path(number):
    """Allow Render secret mounts without committing private checkpoint data."""
    local = DIRECTORY / f"account-{number}.json"
    return local if local.is_file() else SECRET_DIRECTORY / f"account-{number}.json"


def require_demo():
    if (
        not settings.INSTITUTION_SCHEMA_ENABLED
        or not settings.ENABLE_INSTITUTION_DEMO_ACCOUNTS
        or settings.DEPLOYMENT_ENV not in {"development", "test"}
        or not settings.INSTITUTION_DEMO_DEPLOYMENT_ID
    ):
        fail(
            503,
            "DEMO_NOT_AVAILABLE",
            "Institution checkpoints are available only in a commissioned test deployment.",
        )


def manifest(registry):
    require_demo()
    try:
        value = CheckpointManifest.model_validate(registry["source_manifest"])
    except ValueError:
        fail(
            503,
            "DEMO_CHECKPOINT_NOT_READY",
            "The immutable checkpoint source has not passed structural validation.",
        )
    # Only private server files define the checkpoint and reset targets.
    # Preserve legacy source prefixes/configuration labels when env knobs are
    # retired; a persistent account must never need recapture or rebinding.
    if (
        value.target_firebase_uid != registry["firebase_uid"]
        or value.manifest_sha256 != registry["source_manifest_hash"]
        or value.account_number != registry["account_number"]
        or value.checkpoint_key != registry["checkpoint_key"]
        or registry["reset_policy"] != reset_policy(value.account_number)
    ):
        fail(
            409,
            "CHECKPOINT_NOT_READY",
            "The checkpoint source does not match this deployment and verified identity.",
        )
    return value


def account_for(firebase_uid, phone=None):
    """Recognize an exact locally commissioned Firebase UID and test phone."""
    for number in BLUEPRINTS:
        if phone is not None and phone != phone_for(number):
            continue
        path = checkpoint_path(number)
        if not path.is_file():
            if phone == phone_for(number):
                if number == 5:
                    return FreshCheckpoint(target_firebase_uid=firebase_uid)
                from demo.institution.sample_data import SampleCheckpoint

                return SampleCheckpoint(number, firebase_uid)
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("target_firebase_uid") != firebase_uid:
                if phone == phone_for(number):
                    fail(
                        409,
                        "DEMO_IDENTITY_MISMATCH",
                        "This demo checkpoint belongs to a different Firebase identity.",
                    )
                continue
            source = CheckpointManifest.model_validate(data)
            if source.account_number != number:
                raise ValueError("Account/file mismatch")
        except (ValueError, OSError):
            fail(
                503,
                "DEMO_CHECKPOINT_NOT_READY",
                "The private checkpoint file is invalid or unreadable.",
            )
        return source
    return None


def save_checkpoint(source):
    """Operator-only atomic install; account nine cannot be rebound or recaptured."""
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    path = DIRECTORY / f"account-{source.account_number}.json"
    if path.exists():
        old = CheckpointManifest.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )
        if old.target_firebase_uid != source.target_firebase_uid:
            raise ValueError(
                "An installed account cannot be rebound to another identity."
            )
        if source.account_number == 9 and old.manifest_sha256 != source.manifest_sha256:
            raise ValueError(
                "Persistent account nine must retain its existing checkpoint binding."
            )
    for number in BLUEPRINTS:
        other = DIRECTORY / f"account-{number}.json"
        if number == source.account_number or not other.exists():
            continue
        saved = CheckpointManifest.model_validate(
            json.loads(other.read_text(encoding="utf-8"))
        )
        if saved.target_firebase_uid == source.target_firebase_uid or set(
            saved.owned_tenant_ids
        ) & set(source.owned_tenant_ids):
            raise ValueError(
                "Demo identities and checkpoint tenant scopes must be disjoint."
            )
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(source.model_dump_json(), encoding="utf-8")
    os.replace(temporary, path)
