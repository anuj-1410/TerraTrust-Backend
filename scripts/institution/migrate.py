"""Explicit additive migration tool. Importing the application never migrates.

--offline prints the exact ordered bundle/checksums without importing credentials.
--check uses a READ ONLY transaction; --apply is the sole write mode.
"""

import argparse
import asyncio
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def bundles():
    groups = {}
    for path in sorted((ROOT / "migrations/institution").glob("*.sql")):
        version = int(path.name[:3])
        groups.setdefault(version, []).append(path)
    if sorted(groups) != list(range(1, max(groups, default=0) + 1)) or not groups:
        raise ValueError("Migration phases must be contiguous from 001.")
    output = []
    for version, files in sorted(groups.items()):
        data = b"".join(p.name.encode() + b"\0" + p.read_bytes() + b"\0" for p in files)
        output.append((version, files, hashlib.sha256(data).hexdigest()))
    return output


def preflight_sql():
    """Reject incompatible/shared-name schemas before the pending DDL runs."""
    tables = sorted(
        {
            name
            for _, paths, _ in bundles()
            for path in paths
            for name in re.findall(
                r"CREATE TABLE (\w+)\s*\(", path.read_text(encoding="utf-8")
            )
        }
    )
    names = ",".join(
        "'" + table + "'" for table in [*tables, "institution_schema_migrations"]
    )
    return (
        """DO $existing_schema$
BEGIN
 IF to_regclass('public.users') IS NULL THEN RAISE EXCEPTION 'Existing public.users table is required; do not run the farmer schema over an existing project.'; END IF;
 IF EXISTS(SELECT 1 FROM unnest(ARRAY['id','firebase_uid','phone_number','full_name','aadhaar_hash','kyc_completed','wallet_address','role','created_at','updated_at']) AS required(name) WHERE NOT EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid='public.users'::regclass AND attname=required.name AND NOT attisdropped)) THEN RAISE EXCEPTION 'Existing users columns differ; review schema before institution setup.'; END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid='public.users'::regclass AND attname='id' AND atttypid='uuid'::regtype AND NOT attisdropped) THEN RAISE EXCEPTION 'Existing users.id must be UUID.'; END IF;
 IF EXISTS(SELECT 1 FROM pg_tables WHERE schemaname='public' AND (tablename LIKE 'institution_%' OR tablename LIKE 'campus_%' OR tablename LIKE 'advisory_%' OR tablename LIKE 'knowledge_%' OR tablename IN ('processing_jobs','job_outbox','audit_events','idempotency_records','review_cases','operator_grants','emission_factors','carbon_methodologies','model_registry','method_parameters','method_quality_policies')) AND tablename NOT IN ("""
        + names
        + """)) THEN RAISE EXCEPTION 'Unexpected table overlaps institution access rules; review before running migrations.'; END IF;
END $existing_schema$;"""
    )


def sql_bundle(min_version=1):
    """SQL Editor equivalent of --apply; same bytes, ledger, lock and transaction."""
    lines = [
        "-- Generated offline. Review the target project and backup before execution.",
        "BEGIN;",
        "SET LOCAL search_path=public,extensions;",
        "SELECT pg_advisory_xact_lock(hashtextextended('tt:institution:migrations',0));",
        "CREATE TABLE IF NOT EXISTS institution_schema_migrations(version integer PRIMARY KEY,checksum text NOT NULL,files text[] NOT NULL,applied_at timestamptz NOT NULL DEFAULT now());",
    ]
    lines.append(preflight_sql())
    for version, paths, checksum in bundles():
        if version < min_version:
            lines.append(
                "DO $required_phase$ BEGIN "
                f"IF NOT EXISTS(SELECT 1 FROM institution_schema_migrations WHERE version={version} AND checksum='{checksum}') THEN "
                f"RAISE EXCEPTION 'Required migration {version:03d} is missing or differs; review the installed bundle.'; "
                "END IF; END $required_phase$;"
            )
            continue
        lines += [
            "DO $phase_guard$ BEGIN",
            f"IF EXISTS(SELECT 1 FROM institution_schema_migrations WHERE version={version} AND checksum<>'{checksum}') THEN",
            f"RAISE EXCEPTION 'Migration {version:03d} checksum differs. Use a forward migration.'; END IF;",
            f"IF NOT EXISTS(SELECT 1 FROM institution_schema_migrations WHERE version={version}) THEN",
        ]
        for path in paths:
            delimiter = "$tt_sql_" + checksum + "$"
            source = path.read_text(encoding="utf-8")
            if delimiter in source:
                raise ValueError("SQL delimiter collision.")
            lines.append(f"EXECUTE {delimiter}\n{source}\n{delimiter};")
        files = ",".join("'" + path.name.replace("'", "''") + "'" for path in paths)
        lines += [
            f"INSERT INTO institution_schema_migrations(version,checksum,files) VALUES({version},'{checksum}',ARRAY[{files}]);",
            "END IF; END $phase_guard$;",
        ]
    lines += [
        "REVOKE ALL ON institution_schema_migrations FROM PUBLIC;",
        "DO $ledger_acl$ BEGIN IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='anon') THEN REVOKE ALL ON institution_schema_migrations FROM anon; END IF; IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN REVOKE ALL ON institution_schema_migrations FROM authenticated; END IF; END $ledger_acl$;",
        "COMMIT;",
        "SELECT version,checksum,files FROM institution_schema_migrations ORDER BY version;",
    ]
    return "\n".join(lines) + "\n"


async def run(apply):
    import asyncpg

    from app.config import settings
    from app.database_ssl import database_ssl_context

    if not settings.DATABASE_URL:
        raise ValueError(
            "DATABASE_URL is required for explicit database verification/application."
        )
    url = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(
        url,
        ssl=database_ssl_context(settings.DATABASE_SSL_CA_FILE),
        server_settings={"search_path": "public,extensions"},
    )
    try:
        async with conn.transaction(readonly=not apply):
            if apply:
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended('tt:institution:migrations',0))"
                )
                await conn.execute(
                    "CREATE TABLE IF NOT EXISTS institution_schema_migrations(version integer PRIMARY KEY,checksum text NOT NULL,files text[] NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())"
                )
            await conn.execute(preflight_sql())
            exists = await conn.fetchval(
                "SELECT to_regclass('institution_schema_migrations') IS NOT NULL"
            )
            ledger = (
                {
                    r["version"]: r["checksum"]
                    for r in await conn.fetch(
                        "SELECT version,checksum FROM institution_schema_migrations"
                    )
                }
                if exists
                else {}
            )
            for version, files, checksum in bundles():
                if version in ledger and ledger[version] != checksum:
                    raise ValueError(
                        f"Migration {version:03d} checksum differs from the applied ledger. Never rewrite deployed migrations."
                    )
                if version in ledger:
                    print(f"{version:03d} APPLIED {checksum}")
                    continue
                if apply:
                    for path in files:
                        await conn.execute(path.read_text(encoding="utf-8"))
                    await conn.execute(
                        "INSERT INTO institution_schema_migrations(version,checksum,files) VALUES($1,$2,$3)",
                        version,
                        checksum,
                        [p.name for p in files],
                    )
                print(f"{version:03d} {'APPLIED' if apply else 'PENDING'} {checksum}")
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument(
        "--sql-output",
        metavar="PATH",
        help="Write a checksummed SQL Editor bundle offline; never connects.",
    )
    args = parser.parse_args()
    if args.sql_output:
        path = Path(args.sql_output)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as output:
            output.write(sql_bundle())
        print("OFFLINE SQL BUNDLE", path)
    elif args.offline:
        for version, paths, checksum in bundles():
            print(f"{version:03d} {checksum} " + ", ".join(p.name for p in paths))
    else:
        asyncio.run(run(args.apply))


if __name__ == "__main__":
    main()
