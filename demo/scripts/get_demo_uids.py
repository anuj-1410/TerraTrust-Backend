"""Print Firebase UIDs for the configured demo phone numbers."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import firebase_admin
from firebase_admin import auth, credentials

from app.google_credentials import resolve_google_credentials_path as resolve_service_credentials_path


def _resolve_credentials_path() -> str:
    return str(resolve_service_credentials_path("firebase"))


def main() -> None:
    credentials_path = _resolve_credentials_path()
    firebase_admin.initialize_app(credentials.Certificate(credentials_path))

    phones = [
        "+919000000001",
        "+919000000002",
        "+919000000003",
        "+919000000004",
    ]

    print()
    print("Paste this block into DEMO_FIREBASE_UIDS in backend/demo/config.py:")
    print()
    print("  DEMO_FIREBASE_UIDS: dict[str, str] = {")

    all_found = True
    for phone in phones:
        try:
            user = auth.get_user_by_phone_number(phone)
            print(f'      "{phone}": "{user.uid}",')
        except auth.UserNotFoundError:
            print(f'      "{phone}": "NOT_FOUND -- log in once with this number first",')
            all_found = False

    print("  }")
    print()
    if not all_found:
        print("Some UIDs are missing. Log in with those numbers and run this script again.")
    else:
        print("All UIDs found. Paste the block above into config.py, then run:")
        print("  python demo/scripts/seed_demo_accounts.py")


if __name__ == "__main__":
    main()
