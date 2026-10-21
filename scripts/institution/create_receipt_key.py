"""Create the local receipt key once; never overwrite or print its contents."""

import base64
import os

from services.shared.receipt_crypto_service import KEY_FILE


def main():
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit("Key already exists; preserve it. No changes made.")
    with os.fdopen(descriptor, "wb") as output:
        output.write(base64.urlsafe_b64encode(os.urandom(32)) + b"\n")
    print(
        "Created .institution-private/receipt.key. Keep the same private file on API instances and in your backup."
    )


if __name__ == "__main__":
    main()
