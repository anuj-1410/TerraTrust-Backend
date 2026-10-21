"""Provision/capture/validate/register real-service development checkpoints.

These commands require explicit operator execution. They never run at startup.
"""

import argparse
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from firebase_admin import auth
from sqlalchemy import text

from app.config import settings
from app.firebase_auth import get_firebase_app
from demo.institution.manifest import CheckpointManifest, ALLOWED, KEYS
from demo.institution.registry import require_demo, manifest, save_checkpoint
from demo.institution.blueprints import BLUEPRINTS, phone_for
from demo.institution.stages import validate_stage
from models.institution.common import digest, normalize, canonical
from repositories.institution.base import Repository, columns
from services.shared.evidence_service import read_bytes, write_bytes
from tasks.institution_tasks import engine


def effective_config():
    return {
        k: getattr(settings, k)
        for k in (
            "INSTITUTION_DEMO_DEPLOYMENT_ID",
            "FIREBASE_PROJECT_ID",
            "ACTIVE_ACCOUNTING_METHOD_VERSION",
            "BIOMASS_MODEL_VERSION",
            "ACTIVE_CORPUS_VERSION",
            "LLM_PROVIDER",
            "LLM_MODEL",
            "EMBEDDING_MODEL",
            "EMBEDDING_DIMENSION",
        )
    }


async def commissioning_actor(repo, firebase_uid):
    profile = await asyncio.to_thread(
        auth.get_user, firebase_uid, app=get_firebase_app()
    )
    if profile.disabled:
        raise ValueError("Commissioning identity is disabled.")
    return await repo.one(
        "SELECT * FROM users WHERE firebase_uid=:uid", {"uid": profile.uid}
    )


async def verified_target(repo, account, firebase_uid):
    target = await asyncio.to_thread(
        auth.get_user, firebase_uid, app=get_firebase_app()
    )
    if target.disabled or target.phone_number != phone_for(account):
        raise ValueError(
            "The actual Firebase UID must match this commissioned test phone/account."
        )
    user = await repo.one(
        "SELECT * FROM users WHERE firebase_uid=:uid",
        {"uid": firebase_uid},
        required=False,
    )
    if user and (
        user["account_domain"] != "INSTITUTION"
        or user["phone_number"] != target.phone_number
    ):
        raise ValueError(
            "Existing identity/domain differs; no conversion is permitted."
        )
    return target, user


async def validate(repo, data):
    value = CheckpointManifest.model_validate(data)
    if (
        value.deployment_id != settings.INSTITUTION_DEMO_DEPLOYMENT_ID
        or value.deployment_env != settings.DEPLOYMENT_ENV
        or value.prerequisites.config_hash != digest(effective_config())
    ):
        raise ValueError("Manifest configuration/deployment differs.")
    await verified_target(repo, value.account_number, value.target_firebase_uid)
    tables = {t.table: t.rows for t in value.tables}
    for field, table in {
        "method_ids": "carbon_methodologies",
        "factor_ids": "emission_factors",
        "model_ids": "model_registry",
        "quality_policy_ids": "method_quality_policies",
        "corpus_ids": "knowledge_corpus_versions",
    }.items():
        for rid in getattr(value.prerequisites, field):
            row = await repo.get(table, rid)
            if table != "method_quality_policies" and row["status"] not in {
                "APPROVED",
                "ACTIVE",
                "SUPERSEDED",
            }:
                raise ValueError("A required immutable source is unavailable.")
    expected_objects = {
        (row["private_bucket"], row["object_key"]): row
        for table in ("institution_audit_evidence", "institution_artifacts")
        for row in tables.get(table, [])
        if row.get("state", row.get("status")) == "READY"
        and not row.get("bytes_deleted_at")
    }
    actual_objects = {(o.bucket, o.object_key): o for o in value.objects}
    if expected_objects.keys() != actual_objects.keys():
        raise ValueError(
            "Every READY unpurged private file needs exact immutable bytes."
        )
    for key, row in expected_objects.items():
        obj = actual_objects[key]
        data = await read_bytes(obj.bucket, obj.source_object_key)
        if (
            hashlib.sha256(data).hexdigest() != obj.sha256
            or len(data) != obj.size_bytes
            or row["byte_hash"] != obj.sha256
        ):
            raise ValueError("Source bytes do not match manifest/row checksums.")
    for case in tables.get("review_cases", []):
        reviewer = str(case["assigned_reviewer"])
        conflicts = case["resource_snapshot"].get("contributor_user_ids", []) + case[
            "resource_snapshot"
        ].get("historical_member_user_ids", [])
        if reviewer in conflicts or not case["decision"]:
            raise ValueError("Captured review lacks independent approval lineage.")
        await repo.one("SELECT id FROM users WHERE id=:id", {"id": reviewer})
    for member in tables.get("institution_users", []):
        if member["user_id"] != value.target_user_id:
            raise ValueError(
                "Destructive checkpoint targets cannot include unrelated members."
            )
    if value.account_number >= 6:
        owners = [
            m
            for m in tables.get("institution_users", [])
            if m["permission"] == "OWNER" and m["status"] == "ACTIVE"
        ]
        if not owners or any(
            i["verification_status"] != "APPROVED"
            for i in tables.get("institutions", [])
        ):
            raise ValueError(
                "Checkpoint requires actual approved tenant and active owner."
            )
    validate_stage(value.account_number, tables)
    if value.account_number == 9:
        audits = {r["id"]: r for r in tables.get("institution_audits", [])}
        final = audits[value.expected.current_final_audit_id]
        if final["business_status"] != "FINALIZED" or any(
            r["business_status"] in {"DRAFT", "SUBMITTED", "UNDER_REVIEW"}
            for r in audits.values()
        ):
            raise ValueError(
                "Complete checkpoint cannot contain unresolved annual revisions."
            )
        results = [
            r
            for r in tables.get("institution_audit_results", [])
            if r["audit_id"] == final["id"]
            and r["result_hash"] == value.expected.result_hash
        ]
        exports = [
            r
            for r in tables.get("institution_exports", [])
            if r["id"] in value.expected.export_ids and r["status"] == "SUCCEEDED"
        ]
        if not results or {r["format"] for r in exports} != {"PDF", "CSV", "JSON"}:
            raise ValueError(
                "Complete checkpoint requires exact final result and all exports; advisor uses the normal optional service."
            )
    return value


async def capture(repo, args, actor, user):
    await repo.one("SELECT id FROM users WHERE id=:uid FOR UPDATE", {"uid": user["id"]})
    output = Path(args.output).resolve()
    if (output / "manifest.json").exists():
        raise ValueError("An immutable source manifest cannot be overwritten.")
    owned_rows = await repo.rows(
        "SELECT i.id FROM institutions i JOIN institution_users iu ON iu.institution_id=i.id WHERE iu.user_id=:uid AND i.created_by=:uid AND iu.permission='OWNER' AND iu.status='ACTIVE' ORDER BY i.id",
        {"uid": user["id"]},
    )
    owned = [str(r["id"]) for r in owned_rows]
    for iid in sorted(owned):
        await repo.get("institutions", iid, lock=True)
    if args.account == 5:
        if (
            owned
            or await repo.find("institution_users", {"user_id": user["id"]})
            or await repo.find("institutions", {"created_by": user["id"]})
        ):
            raise ValueError(
                "The fresh checkpoint requires an actually fresh identity with no tenant or registration history."
            )
    tables, objects = [], []
    for table in sorted(ALLOWED):
        if table == "institutions":
            rows = await repo.rows(
                "SELECT * FROM institutions WHERE id=ANY(CAST(:ids AS uuid[])) ORDER BY id",
                {"ids": owned},
            )
        elif "institution_id" in columns(table):
            rows = await repo.rows(
                f"SELECT {repo.selection(table)} FROM {table} WHERE institution_id=ANY(CAST(:ids AS uuid[])) ORDER BY id",
                {"ids": owned},
            )
        else:
            continue
        if table == "institution_audit_evidence":
            rows += await repo.rows(
                "SELECT e.* FROM institution_audit_evidence e WHERE e.parent_type='REGISTRATION_DRAFT' AND e.owner_user_id=:uid AND EXISTS(SELECT 1 FROM institution_evidence_links l WHERE l.evidence_id=e.id AND l.institution_id=ANY(CAST(:ids AS uuid[]))) ORDER BY id",
                {"uid": user["id"], "ids": owned},
            )
        # PostgreSQL recomputes this generated daterange on insertion. The
        # manifest preserves its exact source dates instead of a driver object.
        rows = [
            {key: value for key, value in row.items() if key != "period_range"}
            for row in rows
        ]
        encoded = normalize(rows)
        if rows:
            tables.append(
                {
                    "table": table,
                    "rows": encoded,
                    "row_hashes": [digest(r) for r in encoded],
                }
            )
        if table in {"institution_audit_evidence", "institution_artifacts"}:
            for row in rows:
                if row.get("state", row.get("status")) != "READY" or row.get(
                    "bytes_deleted_at"
                ):
                    continue
                data = await read_bytes(row["private_bucket"], row["object_key"])
                hashed = hashlib.sha256(data).hexdigest()
                if hashed != row["byte_hash"]:
                    raise ValueError(
                        "Current private bytes differ from their immutable metadata."
                    )
                key = f"institution-checkpoints/{settings.INSTITUTION_DEMO_DEPLOYMENT_ID}/objects/{hashed}"
                await write_bytes(row["private_bucket"], key, data, row["mime_type"])
                objects.append(
                    {
                        "bucket": row["private_bucket"],
                        "object_key": row["object_key"],
                        "mime_type": row["mime_type"],
                        "size_bytes": len(data),
                        "sha256": hashed,
                        "source_object_key": key,
                        "parent_type": row["parent_type"],
                        "parent_id": str(row["parent_id"]),
                    }
                )
    by_table = {b["table"]: b["rows"] for b in tables}
    finals = [
        r
        for r in by_table.get("institution_audits", [])
        if r["business_status"] == "FINALIZED"
    ]
    final = max(finals, key=lambda r: r["period_end"]) if finals else None
    registration = next(iter(by_table.get("institutions", [])), {}).get(
        "current_registration_id"
    )
    prerequisites = {
        "method_ids": sorted(
            {
                str(r["method_version"])
                for r in by_table.get("institution_audits", [])
                + by_table.get("campus_tree_audits", [])
            }
        ),
        "factor_ids": sorted(
            {
                str(r["factor_id"])
                for r in by_table.get("institution_emission_inputs", [])
                if r["factor_id"]
            }
        ),
        "model_ids": sorted(
            {str(r["model_version"]) for r in by_table.get("campus_tree_audits", [])}
        ),
        "quality_policy_ids": sorted(
            {
                str(r["quality_policy_version"])
                for r in by_table.get("campus_plot_sets", [])
            }
        ),
        "corpus_ids": sorted(
            {r["versions"]["corpus"] for r in by_table.get("advisory_queries", [])}
        ),
        "config_hash": digest(effective_config()),
    }
    expected = {
        "landing_screen": BLUEPRINTS[args.account]["landing"],
        "permission": "OWNER" if args.account >= 6 else None,
        "tenant_count": len(owned),
        "campus_count": len(by_table.get("campuses", [])),
        "survey_count": len(by_table.get("campus_tree_surveys", [])),
        "tree_audit_count": len(by_table.get("campus_tree_audits", [])),
        "audit_count": len(by_table.get("institution_audits", [])),
        "current_final_audit_id": final["id"] if final else None,
        "result_hash": next(
            (
                r["result_hash"]
                for r in by_table.get("institution_audit_results", [])
                if final and r["audit_id"] == final["id"]
            ),
            None,
        ),
        "export_ids": [r["id"] for r in by_table.get("institution_exports", [])],
        "published_query_ids": [
            r["id"]
            for r in by_table.get("advisory_queries", [])
            if r["publication_state"] == "PUBLISHED"
        ],
    }
    value = {
        "schema_version": 1,
        "manifest_id": str(uuid4()),
        "manifest_version": args.manifest_version,
        "checkpoint_key": KEYS[args.account],
        "account_number": args.account,
        "target_firebase_uid": args.firebase_uid,
        "target_user_id": str(user["id"]),
        "deployment_id": settings.INSTITUTION_DEMO_DEPLOYMENT_ID,
        "deployment_env": settings.DEPLOYMENT_ENV,
        "owned_tenant_ids": owned,
        "created_at": normalize(datetime.now(timezone.utc)),
        "created_by": str(actor["id"]),
        "prerequisites": prerequisites,
        "user_checkpoint": {
            "onboarding_status": user["institution_onboarding_status"],
            "onboarding_completed_at": normalize(
                user["institution_onboarding_completed_at"]
            ),
            "registration_id": registration,
            "registration_institution_id": owned[0] if registration else None,
        },
        "tables": tables,
        "objects": objects,
        "expected": expected,
        "provenance": {
            "prepared_via_normal_services": True,
            "review_case_ids": [r["id"] for r in by_table.get("review_cases", [])],
            "source_record_ids": [
                r["id"]
                for r in by_table.get("campus_tree_measurements", [])
                + by_table.get("institution_emission_inputs", [])
            ],
            "validation_record_id": str(uuid4()),
        },
    }
    value["manifest_sha256"] = digest(value)
    await validate(repo, value)
    output.mkdir(parents=True, exist_ok=True)
    (output / "manifest.json").write_bytes(canonical(value))
    print("CAPTURED", value["manifest_id"], value["manifest_sha256"])


async def run(args):
    require_demo()
    db = engine()
    try:
        async with db.begin() as conn:
            repo = Repository(conn)
            actor = await commissioning_actor(repo, args.actor_firebase_uid)
            if args.command == "export-legacy":
                found = await repo.one(
                    "SELECT to_regclass('public.demo_checkpoint_registry') IS NOT NULL AS present"
                )
                if not found["present"]:
                    print("No legacy demo tables remain; no export needed.")
                    return
                rows = await repo.rows(
                    "SELECT * FROM demo_checkpoint_registry ORDER BY account_number"
                )
                for row in rows:
                    if row["state"] == "RESTORING":
                        raise ValueError(
                            "Stop active restores before exporting checkpoints."
                        )
                    source = manifest(row)
                    user = await repo.one(
                        "SELECT id,phone_number,account_domain FROM users WHERE firebase_uid=:uid",
                        {"uid": source.target_firebase_uid},
                    )
                    if (
                        str(user["id"]) != source.target_user_id
                        or user["phone_number"] != phone_for(source.account_number)
                        or user["account_domain"] != "INSTITUTION"
                    ):
                        raise ValueError(
                            "Legacy checkpoint identity does not match the normal user."
                        )
                    save_checkpoint(source)
                print(
                    "EXPORTED",
                    len(rows),
                    "private local checkpoints; database unchanged.",
                )
                return
            if args.command == "provision-user":
                target, user = await verified_target(
                    repo, args.account, args.firebase_uid
                )
                if not user and args.apply:
                    await conn.execute(
                        text(
                            "INSERT INTO users(firebase_uid,phone_number,role,account_domain,institution_onboarding_status,kyc_completed) VALUES(:uid,:phone,'INSTITUTION_ADMIN','INSTITUTION','REQUIRED',false)"
                        ),
                        {"uid": target.uid, "phone": target.phone_number},
                    )
                print(
                    "APPLY" if args.apply else "DRY RUN",
                    "account",
                    args.account,
                    "existing" if user else "new identity",
                )
                return
            if args.command == "capture":
                _, user = await verified_target(repo, args.account, args.firebase_uid)
                if not user:
                    raise ValueError(
                        "Provision the verified institution identity first."
                    )
                await capture(repo, args, actor, user)
                return
            path = Path(args.manifest).resolve()
            source = await validate(repo, json.loads(path.read_text(encoding="utf-8")))
            verification = path.parent / (
                source.provenance.validation_record_id + ".json"
            )
            if args.command == "validate":
                report = {
                    "validation_record_id": source.provenance.validation_record_id,
                    "manifest_id": source.manifest_id,
                    "manifest_sha256": source.manifest_sha256,
                    "verified_at": normalize(datetime.now(timezone.utc)),
                    "verifier": str(actor["id"]),
                    "checks": {"structural_sources_objects_stage": True},
                    "limitations": [
                        "Live restore concurrency and mobile acceptance require the separate Part 18.5 verification record."
                    ],
                }
                verification.write_bytes(canonical(report))
                print("VALIDATED", source.manifest_id, source.manifest_sha256)
                return
            if (
                not verification.exists()
                or json.loads(verification.read_text())["manifest_sha256"]
                != source.manifest_sha256
            ):
                raise ValueError(
                    "Validate this exact immutable manifest before registering it."
                )
            if args.apply:
                save_checkpoint(source)
            print(
                "REGISTERED" if args.apply else "DRY RUN",
                source.manifest_id,
                source.manifest_sha256,
            )
    finally:
        await db.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    legacy = sub.add_parser("export-legacy")
    provision = sub.add_parser("provision-user")
    capture_parser = sub.add_parser("capture")
    for p in (provision, capture_parser):
        p.add_argument("--account", type=int, choices=sorted(BLUEPRINTS), required=True)
        p.add_argument("--firebase-uid", required=True)
    capture_parser.add_argument("--manifest-version", type=int, required=True)
    capture_parser.add_argument("--output", required=True)
    validation = sub.add_parser("validate")
    register = sub.add_parser("register")
    for p in (validation, register):
        p.add_argument("--manifest", required=True)
    for p in (provision, register):
        mode = p.add_mutually_exclusive_group(required=True)
        mode.add_argument("--dry-run", action="store_true")
        mode.add_argument("--apply", action="store_true")
    for command in (provision, capture_parser, validation, register, legacy):
        command.add_argument("--actor-firebase-uid", required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
