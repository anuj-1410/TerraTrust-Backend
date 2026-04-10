"""IPFS service — upload audit metadata JSON to Pinata."""

import logging
from typing import Any, Dict

import httpx

from app.config import settings

logger = logging.getLogger("terratrust.ipfs")


def _pinata_headers() -> Dict[str, str]:
    """Return authenticated Pinata headers, failing clearly when unset."""
    if not settings.PINATA_JWT:
        raise RuntimeError("PINATA_JWT is not configured.")

    return {
        "Authorization": f"Bearer {settings.PINATA_JWT}",
        "Content-Type": "application/json",
    }


async def upload_to_ipfs(metadata: Dict[str, Any], audit_id: str = "") -> str:
    """Pin a JSON object to IPFS via Pinata.

    Parameters
    ----------
    metadata : dict
        Arbitrary JSON-serialisable metadata to pin.
    audit_id : str, optional
        Used for the ``pinataMetadata.name`` field.

    Returns
    -------
    str
        IPFS URI in the form ``ipfs://<CID>``.
    """
    url = "https://api.pinata.cloud/pinning/pinJSONToIPFS"
    headers = _pinata_headers()
    body = {
        "pinataOptions": {"cidVersion": 1},
        "pinataContent": metadata,
        "pinataMetadata": {"name": f"terratrust-audit-{audit_id}"},
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    cid = data["IpfsHash"]
    ipfs_url = f"ipfs://{cid}"
    logger.info("Pinned metadata to IPFS: %s", ipfs_url)
    return ipfs_url


def to_gateway_url(ipfs_url: str | None) -> str | None:
    """Convert an ``ipfs://`` URI into the configured Pinata gateway URL."""
    if not ipfs_url:
        return None
    if ipfs_url.startswith("http://") or ipfs_url.startswith("https://"):
        return ipfs_url
    if not settings.PINATA_GATEWAY_URL:
        return ipfs_url

    cid = ipfs_url.strip()
    if cid.startswith("ipfs://"):
        cid = cid.removeprefix("ipfs://")
    elif cid.startswith("/ipfs/"):
        cid = cid.removeprefix("/ipfs/")
    elif cid.startswith("ipfs/"):
        cid = cid.removeprefix("ipfs/")

    if not cid:
        return None

    return f"https://{settings.PINATA_GATEWAY_URL}/ipfs/{cid}"
