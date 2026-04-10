"""Minting service for Pinata metadata packaging and Polygon minting."""

import hashlib
import json
import logging
import math
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from eth_account import Account
from web3 import Web3

from app.config import settings
from services.ipfs_service import upload_to_ipfs

logger = logging.getLogger("terratrust.minting")
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
MINT_RECOVERY_LOOKBACK_BLOCKS = 10_000
MINT_TRANSACTION_GAS_LIMIT = 500_000
MINT_GAS_RETRY_MULTIPLIER = 1.20

# ---------------------------------------------------------------------------
# Contract ABI paths
# ---------------------------------------------------------------------------
CONTRACT_ARTIFACT_RELATIVE_PATHS = (
    os.path.join("contracts", "TerraToken.sol", "TerraTrustToken.json"),
    os.path.join("contracts", "TerraToken.sol", "TerraToken.json"),
    "TerraTrustToken_ABI.json",
    "TerraToken_ABI.json",
)


def _coerce_credit_amount(credit_amount: float) -> int:
    """Convert a precise credit quantity into raw integer deci-CTT units."""
    try:
        precise_amount = float(credit_amount)
    except (TypeError, ValueError) as exc:
        raise ValueError("credit_amount must be numeric.") from exc

    # The contract stores integer tenths of a CTT. Truncate rather than round
    # so the on-chain amount never exceeds the verified CO2e quantity.
    coerced_amount = max(0, int(math.floor((precise_amount * 10) + 1e-9)))
    truncated_display_amount = coerced_amount / 10
    if abs(precise_amount - truncated_display_amount) > 1e-9:
        logger.warning(
            "Truncating calculated credits %.4f to %.1f CTT (%d raw deci-CTT units) for ERC-1155 minting.",
            precise_amount,
            truncated_display_amount,
            coerced_amount,
        )
    return coerced_amount


def _derive_measurement_date(
    tree_scans: List[Dict[str, Any]],
    fallback_timestamp: Any = None,
) -> str:
    """Return the documented measurement date from scan timestamps when available."""
    candidates: List[datetime] = []

    def _coerce_timestamp(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

        if isinstance(value, str):
            candidate = value.strip()
            if not candidate:
                return None
            if candidate.endswith("Z"):
                candidate = f"{candidate[:-1]}+00:00"
            try:
                parsed = datetime.fromisoformat(candidate)
            except ValueError:
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

        return None

    for scan in tree_scans:
        parsed_timestamp = _coerce_timestamp(scan.get("scan_timestamp"))
        if parsed_timestamp is not None:
            candidates.append(parsed_timestamp.astimezone(timezone.utc))

    if candidates:
        return max(candidates).date().isoformat()

    parsed_fallback = _coerce_timestamp(fallback_timestamp)
    if parsed_fallback is not None:
        return parsed_fallback.astimezone(timezone.utc).date().isoformat()

    return datetime.now(timezone.utc).date().isoformat()


def _resolve_artifact_path() -> str:
    """Return the first available contract artifact path for the deployed token contract."""
    artifacts_dir = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "contracts",
        "artifacts",
    )
    candidate_paths = [
        os.path.join(artifacts_dir, relative_path)
        for relative_path in CONTRACT_ARTIFACT_RELATIVE_PATHS
    ]

    for candidate in candidate_paths:
        if os.path.exists(candidate):
            return candidate

    expected_paths = ", ".join(candidate_paths)
    raise FileNotFoundError(
        "Contract ABI not found. Expected one of: " + expected_paths
    )


def load_contract_abi() -> list[dict[str, Any]]:
    """Load the TerraTrust token ABI from the Hardhat artifact tree."""
    artifact_path = _resolve_artifact_path()
    with open(artifact_path, "r", encoding="utf-8") as file_handle:
        artifact = json.load(file_handle)

    if isinstance(artifact, dict) and isinstance(artifact.get("abi"), list):
        return artifact["abi"]
    if isinstance(artifact, list):
        return artifact

    raise ValueError(f"Contract artifact '{artifact_path}' does not contain a valid ABI.")


def _require_setting(name: str, value: str) -> str:
    """Require a non-empty runtime setting value."""
    if not value:
        raise RuntimeError(f"{name} is not configured.")
    return value


def _scale_gas_price(base_gas_price: int, multiplier: float = 1.0) -> int:
    """Scale a gas price by a positive multiplier and round up safely."""
    if base_gas_price <= 0:
        raise ValueError("base_gas_price must be positive.")
    if multiplier <= 0:
        raise ValueError("multiplier must be positive.")
    return max(1, int(math.ceil(base_gas_price * multiplier)))


def _get_polygon_client() -> tuple[Web3, Any]:
    """Return the configured Web3 client and admin account."""
    rpc_url = _require_setting(
        "ALCHEMY_POLYGON_AMOY_URL",
        settings.ALCHEMY_POLYGON_AMOY_URL,
    )
    contract_address = _require_setting("CONTRACT_ADDRESS", settings.CONTRACT_ADDRESS)
    private_key = _require_setting("ADMIN_WALLET_PRIVATE_KEY", settings.ADMIN_WALLET_PRIVATE_KEY)

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        raise ConnectionError("Cannot connect to Polygon RPC.")
    if not Web3.is_address(contract_address):
        raise RuntimeError("CONTRACT_ADDRESS is not a valid EVM address.")

    admin_account = Account.from_key(private_key)
    configured_admin_address = settings.ADMIN_WALLET_ADDRESS.strip()
    if configured_admin_address and admin_account.address.lower() != configured_admin_address.lower():
        raise RuntimeError(
            "ADMIN_WALLET_ADDRESS does not match ADMIN_WALLET_PRIVATE_KEY."
        )

    return w3, admin_account


def _build_contract(w3: Web3) -> Any:
    """Instantiate the deployed TerraTrust token contract."""
    return w3.eth.contract(
        address=Web3.to_checksum_address(settings.CONTRACT_ADDRESS),
        abi=load_contract_abi(),
    )


def _audit_mint_key(land_id: str, audit_year: int) -> bytes:
    """Compute the contract's double-mint prevention key."""
    return Web3.solidity_keccak(["string", "uint256"], [land_id, audit_year])


def _recover_existing_mint(
    w3: Web3,
    contract: Any,
    farmer_address: str,
    audit_id_int: int,
    land_id: str,
    audit_year: int,
) -> Dict[str, Any] | None:
    """Recover an already-mined audit by inspecting contract state and recent logs."""
    if not contract.functions.auditMinted(_audit_mint_key(land_id, audit_year)).call():
        return None

    ipfs_url = contract.functions.getAuditEvidence(audit_id_int).call() or None
    latest_block = int(w3.eth.block_number)
    from_block = max(0, latest_block - MINT_RECOVERY_LOOKBACK_BLOCKS)
    transfer_logs = contract.events.TransferSingle().get_logs(
        from_block=from_block,
        to_block=latest_block,
        argument_filters={
            "from": ZERO_ADDRESS,
            "to": farmer_address,
        },
    )

    for event in reversed(transfer_logs):
        if int(event["args"]["id"]) != int(audit_id_int):
            continue

        return {
            "tx_hash": event["transactionHash"].hex(),
            "ipfs_url": ipfs_url,
            "block_number": event["blockNumber"],
            "credit_amount": None,
        }

    if ipfs_url:
        logger.warning(
            "Recovered audit %s from contract state without a recent TransferSingle log.",
            audit_id_int,
        )
        return {
            "tx_hash": None,
            "ipfs_url": ipfs_url,
            "block_number": None,
            "credit_amount": None,
        }

    return None


def recover_existing_mint(
    farmer_address: str,
    audit_id_int: int,
    land_id: str,
    audit_year: int,
) -> Dict[str, Any] | None:
    """Public helper used by Celery retries to recover an existing on-chain mint."""
    if not Web3.is_address(farmer_address):
        raise RuntimeError("farmer_address is not a valid EVM address.")

    w3, _admin_account = _get_polygon_client()
    contract = _build_contract(w3)
    return _recover_existing_mint(
        w3,
        contract,
        Web3.to_checksum_address(farmer_address),
        audit_id_int,
        land_id,
        audit_year,
    )


def _submit_mint_transaction(
    w3: Web3,
    contract: Any,
    admin_account: Any,
    farmer_checksum: str,
    audit_id_int: int,
    credit_amount_int: int,
    ipfs_url: str,
    land_id: str,
    audit_year: int,
    nonce: int,
    gas_price_multiplier: float,
) -> Dict[str, Any]:
    """Build, sign, submit, and confirm a mint transaction."""
    gas_price = _scale_gas_price(int(w3.eth.gas_price), gas_price_multiplier)

    tx = contract.functions.mintAudit(
        farmer_checksum,
        audit_id_int,
        credit_amount_int,
        ipfs_url,
        land_id,
        audit_year,
    ).build_transaction(
        {
            "from": admin_account.address,
            "nonce": nonce,
            "gas": MINT_TRANSACTION_GAS_LIMIT,
            "gasPrice": gas_price,
            "chainId": w3.eth.chain_id,
        }
    )

    signed = admin_account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status != 1:
        raise RuntimeError(f"Polygon mint transaction failed: {receipt.transactionHash.hex()}")

    return {
        "tx_hash": receipt.transactionHash.hex(),
        "ipfs_url": ipfs_url,
        "block_number": receipt.blockNumber,
        "credit_amount": credit_amount_int,
    }


# ---------------------------------------------------------------------------
# Build evidence metadata
# ---------------------------------------------------------------------------
def build_audit_metadata(
    audit_data: Dict[str, Any],
    tree_scans: List[Dict[str, Any]],
    credit_result: Dict[str, Any],
) -> Dict[str, Any]:
    """Assemble the complete on-chain evidence package.

    This metadata is pinned to IPFS and referenced in the NFT
    certificate minted alongside the fungible credit tokens.

    Parameters
    ----------
    audit_data : dict
        Core audit record (land_id, survey_number, district, etc.).
    tree_scans : list[dict]
        AR tree scan records.
    credit_result : dict
        Output of ``fusion_engine.calculate_credits()``.

    Returns
    -------
    dict
        Full evidence metadata ready for IPFS pinning.
    """
    boundary_geojson = audit_data.get("boundary_geojson") or {}
    boundary_hash = None
    if boundary_geojson:
        boundary_hash = hashlib.sha256(
            json.dumps(boundary_geojson, sort_keys=True).encode("utf-8")
        ).hexdigest()

    satellite_data = credit_result.get("satellite_features", {})
    return {
        "name": (
            f"TerraTrust Audit - Farm {audit_data.get('land_id')}, "
            f"Year {audit_data.get('audit_year')}"
        ),
        "land_id": audit_data.get("land_id"),
        "survey_number": audit_data.get("survey_number"),
        "district": audit_data.get("district"),
        "taluka": audit_data.get("taluka"),
        "village": audit_data.get("village"),
        "land_boundary_hash": boundary_hash,
        "audit_year": audit_data.get("audit_year"),
        "measurement_date": _derive_measurement_date(
            tree_scans,
            audit_data.get("calculated_at") or audit_data.get("created_at"),
        ),
        "total_biomass_tonnes": credit_result.get("current_biomass", 0),
        "prev_biomass_tonnes": credit_result.get("prev_year_biomass", 0),
        "delta_biomass": credit_result.get("delta_biomass", 0),
        "carbon_tonnes": credit_result.get("carbon_tonnes", 0),
        "co2_equivalent": credit_result.get("co2_equivalent", 0),
        "credits_issued": credit_result.get("credits_issued", 0),
        "satellite_data": {
            "sentinel1_vh_mean_db": satellite_data.get("s1_vh_mean_db", 0),
            "sentinel1_vv_mean_db": satellite_data.get("s1_vv_mean_db", 0),
            "sentinel1_vh_vv_ratio_mean": satellite_data.get("s1_vh_vv_ratio_mean", 0),
            "sentinel2_ndvi_mean": satellite_data.get("s2_ndvi_mean", 0),
            "sentinel2_evi_mean": satellite_data.get("s2_evi_mean", 0),
            "sentinel2_red_edge_mean": satellite_data.get("s2_red_edge_mean", 0),
            "gedi_height_mean": satellite_data.get("gedi_height_mean", 0),
            "srtm_elevation_mean": satellite_data.get("srtm_elevation_mean", 0),
            "srtm_slope_mean": satellite_data.get("srtm_slope_mean", 0),
            "nisar_used": satellite_data.get("nisar_used", False),
            "features_count": satellite_data.get("features_count", 0),
            "processing_method": satellite_data.get("processing_method", "S1_S2_GEDI_SRTM_XGBoost_v3.1"),
        },
        "tree_samples": [
            {
                "species": t.get("species"),
                "species_source": t.get("species_source"),
                "dbh_cm": t.get("dbh_cm"),
                "height_source": t.get("height_source") or ("GEDI" if t.get("gedi_height_m") else "AR_FALLBACK"),
                "height_m": t.get("gedi_height_m") or t.get("height_m"),
                "agb_kg": t.get("agb_kg"),
                "scan_timestamp": t.get("scan_timestamp"),
                "measurement_tier": t.get("ar_tier_used"),
                "gps": t.get("gps"),
                "evidence_photo_hash": t.get("evidence_photo_hash"),
            }
            for t in tree_scans
        ],
        "calculation_method": satellite_data.get("processing_method", "S1_S2_GEDI_SRTM_XGBoost_v3.1"),
        "boundary_verification_method": audit_data.get("boundary_source"),
    }


# ---------------------------------------------------------------------------
# Mint on Polygon
# ---------------------------------------------------------------------------
async def mint_carbon_credits(
    farmer_address: str,
    audit_id_int: int,
    credit_amount: float,
    metadata: Dict[str, Any],
    land_id: str,
    audit_year: int,
) -> Dict[str, Any]:
    """Mint carbon credits and an NFT certificate on Polygon.

    Steps
    -----
    1. Upload evidence metadata to IPFS via Pinata.
    2. Connect to Polygon via Alchemy RPC.
    3. Call ``TerraTrustToken.mintAudit()`` with the admin account.
    4. Wait for the transaction receipt.

    Parameters
    ----------
    farmer_address : str
        Farmer's Polygon wallet address.
    audit_id_int : int
        Numeric audit ID for the ERC-1155 NFT token id.
    credit_amount : float
        Calculated carbon credit quantity. The on-chain mint uses whole-token units.
    metadata : dict
        Full audit evidence metadata.
    land_id : str
        UUID of the land parcel (used for double-mint key).
    audit_year : int
        Calendar year of the audit.

    Returns
    -------
    dict
        ``{tx_hash, ipfs_url, block_number, credit_amount}``
    """
    credit_amount_int = _coerce_credit_amount(credit_amount)
    if not Web3.is_address(farmer_address):
        raise RuntimeError("farmer_address is not a valid EVM address.")

    farmer_checksum = Web3.to_checksum_address(farmer_address)

    # 1. Connect to Polygon and recover any previously mined audit before pinning
    # duplicate metadata or attempting a second mint.
    w3, admin_account = _get_polygon_client()
    contract = _build_contract(w3)
    existing_mint = _recover_existing_mint(
        w3,
        contract,
        farmer_checksum,
        audit_id_int,
        land_id,
        audit_year,
    )
    if existing_mint is not None:
        existing_mint["credit_amount"] = credit_amount_int
        logger.warning(
            "Recovered existing on-chain mint for audit %s instead of minting again.",
            audit_id_int,
        )
        return existing_mint

    metadata_payload = dict(metadata)
    metadata_payload["minted_credit_amount"] = credit_amount_int
    if abs(float(metadata_payload.get("credits_issued", 0) or 0) - (credit_amount_int / 10)) > 1e-9:
        metadata_payload["minting_note"] = (
            "CTT are minted as integer deci-CTT ERC-1155 units on-chain."
        )

    # 2. Pin metadata to IPFS
    ipfs_url = await upload_to_ipfs(metadata_payload, audit_id=str(audit_id_int))

    nonce = w3.eth.get_transaction_count(admin_account.address)
    last_exc: Exception | None = None
    for attempt_number, gas_multiplier in enumerate((1.0, MINT_GAS_RETRY_MULTIPLIER), start=1):
        try:
            result = _submit_mint_transaction(
                w3=w3,
                contract=contract,
                admin_account=admin_account,
                farmer_checksum=farmer_checksum,
                audit_id_int=audit_id_int,
                credit_amount_int=credit_amount_int,
                ipfs_url=ipfs_url,
                land_id=land_id,
                audit_year=audit_year,
                nonce=nonce,
                gas_price_multiplier=gas_multiplier,
            )
            logger.info(
                "Minted %d raw deci-CTT units (%.1f CTT) → tx %s",
                credit_amount_int,
                credit_amount_int / 10,
                result["tx_hash"],
            )
            return result
        except Exception as exc:
            recovered_mint = _recover_existing_mint(
                w3,
                contract,
                farmer_checksum,
                audit_id_int,
                land_id,
                audit_year,
            )
            if recovered_mint is not None:
                recovered_mint["credit_amount"] = credit_amount_int
                logger.warning(
                    "Recovered existing on-chain mint for audit %s after a transaction error.",
                    audit_id_int,
                )
                return recovered_mint

            last_exc = exc
            if attempt_number >= 2:
                break

            logger.warning(
                "Mint transaction attempt %d failed for audit %s: %s. Retrying once with a 20%% gas-price increase.",
                attempt_number,
                audit_id_int,
                exc,
            )

    if last_exc is None:
        raise RuntimeError("Mint transaction failed before an exception was captured.")

    raise last_exc
