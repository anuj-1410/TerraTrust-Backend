"""Explicit, audited operator-grant bootstrap. Never changes farmer roles/domain."""
import argparse
import asyncio
from datetime import datetime, timezone

from firebase_admin import auth

from app.firebase_auth import get_firebase_app
from app.institution_access import CASE_CAPABILITIES, REGISTRY_CAPABILITIES
from models.institution.common import digest
from repositories.institution.base import Repository
from tasks.institution_tasks import engine

CAPABILITIES = set(CASE_CAPABILITIES.values()) | set(REGISTRY_CAPABILITIES.values()) | {"REGISTRATION_OPERATIONS", "PRIVACY_OPERATIONS"}


async def run(args):
    if args.capability not in CAPABILITIES or not args.reason.strip():
        raise ValueError("Supply an approved capability and change-record reason.")
    expiry = datetime.fromisoformat(args.expires_at.replace("Z", "+00:00"))
    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
        raise ValueError("Grant expiry must be an explicit future UTC timestamp.")
    target = await asyncio.to_thread(auth.get_user, args.firebase_uid, app=get_firebase_app())
    grantor = await asyncio.to_thread(auth.get_user, args.grantor_firebase_uid, app=get_firebase_app())
    if target.disabled or grantor.disabled or not target.phone_number or not grantor.phone_number:
        raise ValueError("Target and grantor must be enabled verified phone identities in this Firebase project.")
    db = engine()
    try:
        async with db.begin() as conn:
            repo = Repository(conn)
            user = await repo.one("SELECT * FROM users WHERE firebase_uid=:uid", {"uid": target.uid})
            author = await repo.one("SELECT * FROM users WHERE firebase_uid=:uid", {"uid": grantor.uid})
            if user["phone_number"] != target.phone_number or author["phone_number"] != grantor.phone_number:
                raise ValueError("Firebase and relational identity do not match.")
            if args.institution_id:
                await repo.get("institutions", args.institution_id)
            if args.case_id:
                case = await repo.get("review_cases", args.case_id)
                if args.institution_id and str(case["institution_id"]) != args.institution_id:
                    raise ValueError("Case and tenant scope differ.")
            if args.capability in CASE_CAPABILITIES.values() and not (args.institution_id or args.case_id):
                raise ValueError("Review capabilities require explicit tenant or case scope.")
            if args.capability in set(REGISTRY_CAPABILITIES.values()) | {"PRIVACY_OPERATIONS", "REGISTRATION_OPERATIONS"} and (args.institution_id or args.case_id):
                raise ValueError("This capability has global operational scope.")
            proposed = {"user_id": str(user["id"]), "capability": args.capability, "institution_id": args.institution_id, "case_id": args.case_id, "valid_until": expiry, "granted_by": str(author["id"])}
            print("APPLY" if args.apply else "DRY RUN", digest(proposed), args.capability, str(user["id"]))
            if args.apply:
                row = await repo.insert("operator_grants", {**proposed, "valid_from": datetime.now(timezone.utc)})
                await repo.event(author, "OPERATOR_GRANT_CREATED", "operator_grants", row, reason=args.reason)
    finally:
        await db.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firebase-uid", required=True)
    parser.add_argument("--grantor-firebase-uid", required=True)
    parser.add_argument("--capability", required=True)
    parser.add_argument("--institution-id")
    parser.add_argument("--case-id")
    parser.add_argument("--expires-at", required=True)
    parser.add_argument("--reason", required=True)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--apply", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
