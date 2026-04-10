import os
import sys
import types

os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")

eth_account_stub = types.ModuleType("eth_account")


class _AccountStub:
    @staticmethod
    def from_key(_value):
        return object()


eth_account_stub.Account = _AccountStub
sys.modules.setdefault("eth_account", eth_account_stub)

web3_stub = types.ModuleType("web3")


class _Web3Stub:
    HTTPProvider = object

    def __init__(self, *_args, **_kwargs):
        pass


web3_stub.Web3 = _Web3Stub
sys.modules.setdefault("web3", web3_stub)

config_stub = types.ModuleType("app.config")
config_stub.settings = types.SimpleNamespace(
    PINATA_JWT="",
    PINATA_GATEWAY_URL="gateway.pinata.cloud",
    ALCHEMY_POLYGON_AMOY_URL="",
    ADMIN_WALLET_PRIVATE_KEY="",
    ADMIN_WALLET_ADDRESS="",
    CONTRACT_ADDRESS="",
)
sys.modules.setdefault("app.config", config_stub)

ipfs_stub = types.ModuleType("services.ipfs_service")
ipfs_stub.upload_to_ipfs = lambda *_args, **_kwargs: "ipfs://stub"
sys.modules.setdefault("services.ipfs_service", ipfs_stub)

import hashlib
import json

from services.minting_service import _coerce_credit_amount, _scale_gas_price, build_audit_metadata


def test_build_audit_metadata_hashes_boundary_and_preserves_tree_details():
    boundary_geojson = {
        "type": "Polygon",
        "coordinates": [[[73.981, 18.545], [73.982, 18.545], [73.982, 18.546], [73.981, 18.545]]],
    }

    metadata = build_audit_metadata(
        audit_data={
            "land_id": "land-1",
            "survey_number": "47",
            "district": "Pune",
            "taluka": "Haveli",
            "village": "Kharadi",
            "boundary_source": "WMS_AUTO",
            "boundary_geojson": boundary_geojson,
            "audit_year": 2026,
        },
        tree_scans=[
            {
                "species": "Teak",
                "dbh_cm": 22.4,
                "gedi_height_m": 15.3,
                "height_source": "GEDI",
                "agb_kg": 245.6,
                "scan_timestamp": "2026-11-10T10:24:31Z",
                "ar_tier_used": 1,
                "gps": {"lat": 18.546, "lng": 73.981},
                "evidence_photo_hash": "abc123",
            }
        ],
        credit_result={
            "credits_issued": 4.1,
            "prev_year_biomass": 12.4,
            "current_biomass": 14.8,
            "delta_biomass": 2.4,
            "carbon_tonnes": 1.128,
            "co2_equivalent": 4.1,
            "satellite_features": {
                "s1_vh_mean_db": -12.4,
                "s1_vv_mean_db": -9.6,
                "s1_vh_vv_ratio_mean": 1.29,
                "s2_ndvi_mean": 0.68,
                "s2_evi_mean": 0.51,
                "s2_red_edge_mean": 0.34,
                "gedi_height_mean": 14.2,
                "srtm_elevation_mean": 545.0,
                "srtm_slope_mean": 2.1,
                "nisar_used": False,
                "features_count": 9,
                "processing_method": "S1_S2_GEDI_SRTM_XGBoost_v3.1",
            },
        },
    )

    expected_hash = hashlib.sha256(
        json.dumps(boundary_geojson, sort_keys=True).encode("utf-8")
    ).hexdigest()

    assert metadata["land_boundary_hash"] == expected_hash
    assert metadata["measurement_date"] == "2026-11-10"
    assert metadata["tree_samples"][0]["height_source"] == "GEDI"
    assert metadata["tree_samples"][0]["height_m"] == 15.3
    assert metadata["tree_samples"][0]["agb_kg"] == 245.6
    assert metadata["satellite_data"]["sentinel1_vh_mean_db"] == -12.4
    assert metadata["boundary_verification_method"] == "WMS_AUTO"


def test_coerce_credit_amount_truncates_to_deci_ctt_units():
    assert _coerce_credit_amount(4.1) == 41
    assert _coerce_credit_amount(4.69) == 46
    assert _coerce_credit_amount(0.2) == 2


def test_scale_gas_price_rounds_up_for_documented_retry_bump():
    assert _scale_gas_price(100, 1.0) == 100
    assert _scale_gas_price(100, 1.2) == 120
    assert _scale_gas_price(101, 1.2) == 122
