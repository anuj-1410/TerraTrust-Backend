"""Restore private checkpoints into normal institution tables, like farmer resets."""

import hashlib
from datetime import datetime

from sqlalchemy import text

from app.config import settings
from app.institution_errors import fail
from demo.institution.registry import require_demo
from demo.institution.manifest import ALLOWED
from repositories.institution.base import columns
from services.shared.evidence_service import read_bytes, write_bytes
from models.institution.common import digest, normalize


async def restore_to_checkpoint(repo, source, generation):
    """Caller holds the verified identity lock and one database transaction."""
    require_demo()
    if source.account_number == 9:
        fail(
            409,
            "DEMO_PERSISTENT_ACCOUNT",
            "Persistent institution history cannot be reset.",
        )
    # Discover new demo-created tenants from ordinary ownership, not a demo table.
    tenants = await repo.rows(
        "SELECT id FROM institutions WHERE created_by=:uid ORDER BY id FOR UPDATE",
        {"uid": source.target_user_id},
    )
    owned = [str(row["id"]) for row in tenants]
    if not set(source.owned_tenant_ids) <= set(owned):
        fail(
            409,
            "CHECKPOINT_SCOPE_CHANGED",
            "A checkpoint institution is missing or no longer owned by this identity.",
        )
    for iid in owned:
        other = await repo.rows(
            "SELECT user_id FROM institution_users WHERE institution_id=:iid AND user_id<>:uid",
            {"iid": iid, "uid": source.target_user_id},
        )
        if other:
            fail(
                409,
                "CHECKPOINT_SCOPE_CHANGED",
                "A demo institution has another member; resolve that membership before resetting.",
            )
    conn = repo.conn
    await conn.execute(text("SET CONSTRAINTS ALL DEFERRED"))
    await conn.execute(
        text("SELECT set_config('terratrust.demo_restore_uid',:uid,true)"),
        {"uid": source.target_firebase_uid},
    )
    permitted = await repo.one(
        "SELECT tt_demo_restore_allowed('users',jsonb_build_object('id',CAST(:uid AS text),'account_domain','INSTITUTION')) AS allowed",
        {"uid": source.target_user_id},
    )
    if not permitted["allowed"]:
        fail(
            409,
            "CHECKPOINT_SCOPE_CHANGED",
            "Database checkpoint scope is not authorized; apply migration 016.",
        )
    # Verify/copy actual saved bytes. No mock provider or calculated result is made.
    for obj in source.objects:
        if obj.bucket not in {
            settings.PRIVATE_STORAGE_BUCKET,
            settings.REPORT_STORAGE_BUCKET,
        }:
            fail(409, "CHECKPOINT_NOT_READY", "Source bytes use an unsupported bucket.")
        data = await read_bytes(obj.bucket, obj.source_object_key)
        if (
            len(data) != obj.size_bytes
            or hashlib.sha256(data).hexdigest() != obj.sha256
        ):
            fail(
                409,
                "CHECKPOINT_BYTES_CHANGED",
                "Checkpoint bytes are missing or changed.",
            )
        await write_bytes(obj.bucket, obj.object_key, data, obj.mime_type)
    await conn.execute(
        text(
            "DELETE FROM job_outbox WHERE job_id IN (SELECT id FROM processing_jobs WHERE institution_id=ANY(CAST(:ids AS uuid[])))"
        ),
        {"ids": owned},
    )
    await conn.execute(
        text(
            "DELETE FROM idempotency_records WHERE institution_id=ANY(CAST(:ids AS uuid[])) OR user_id=:uid"
        ),
        {"ids": owned, "uid": source.target_user_id},
    )
    await conn.execute(
        text(
            "DELETE FROM institution_registration_authorizations WHERE institution_id=ANY(CAST(:ids AS uuid[])) OR target_user_id=:uid"
        ),
        {"ids": owned, "uid": source.target_user_id},
    )
    removed = [iid for iid in owned if iid not in source.owned_tenant_ids]
    await conn.execute(
        text(
            "DELETE FROM operator_grants WHERE institution_id=ANY(CAST(:ids AS uuid[]))"
        ),
        {"ids": removed},
    )
    await conn.execute(
        text(
            "DELETE FROM institution_provider_operations WHERE job_id IN (SELECT id FROM processing_jobs WHERE institution_id=ANY(CAST(:ids AS uuid[])))"
        ),
        {"ids": owned},
    )
    for table in sorted(ALLOWED - {"institutions"}, reverse=True):
        if "institution_id" in columns(table):
            await conn.execute(
                text(
                    f"DELETE FROM {table} WHERE institution_id=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": owned},
            )
    await conn.execute(
        text("DELETE FROM institutions WHERE id=ANY(CAST(:ids AS uuid[]))"),
        {"ids": owned},
    )
    await conn.execute(
        text(
            "DELETE FROM institution_audit_evidence WHERE parent_type='REGISTRATION_DRAFT' AND parent_id=:uid AND owner_user_id=:uid AND NOT EXISTS(SELECT 1 FROM institution_evidence_links l WHERE l.evidence_id=institution_audit_evidence.id)"
        ),
        {"uid": source.target_user_id},
    )
    # The SQL reset guard checks live ownership even during replay. Restore the
    # institution first; its forward references remain deferred until commit.
    for bundle in sorted(source.tables, key=lambda item: item.table != "institutions"):
        for raw in bundle.rows:
            row = dict(raw)
            if bundle.table == "processing_jobs":
                row["fixture_generation"] = generation
            saved = normalize(await repo.insert(bundle.table, row))
            if digest({key: saved.get(key) for key in row}) != digest(row):
                fail(
                    409,
                    "CHECKPOINT_ROW_MISMATCH",
                    "Restored fields differ from the saved checkpoint.",
                )
    for table in sorted(ALLOWED):
        if table == "institutions":
            actual = await repo.one(
                "SELECT count(*) AS total FROM institutions WHERE id=ANY(CAST(:ids AS uuid[]))",
                {"ids": owned},
            )
        elif "institution_id" in columns(table):
            clause = (
                " OR (parent_type='REGISTRATION_DRAFT' AND owner_user_id=:uid)"
                if table == "institution_audit_evidence"
                else ""
            )
            actual = await repo.one(
                f"SELECT count(*) AS total FROM {table} WHERE institution_id=ANY(CAST(:ids AS uuid[]))"
                + clause,
                {"ids": owned, "uid": source.target_user_id},
            )
        else:
            continue
        expected = next((len(b.rows) for b in source.tables if b.table == table), 0)
        if actual["total"] != expected:
            fail(
                409,
                "CHECKPOINT_COUNT_MISMATCH",
                "Restored counts differ from the saved checkpoint.",
            )
    await conn.execute(
        text(
            "UPDATE users SET institution_onboarding_status=:state,institution_onboarding_completed_at=:completed WHERE id=:uid AND account_domain='INSTITUTION'"
        ),
        {
            "state": source.user_checkpoint.onboarding_status,
            "completed": datetime.fromisoformat(
                source.user_checkpoint.onboarding_completed_at.replace("Z", "+00:00")
            )
            if source.user_checkpoint.onboarding_completed_at
            else None,
            "uid": source.target_user_id,
        },
    )
    await conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
