from demo.checkpoints import checkpoint_full
from demo.config import DEMO_ACCOUNT_BLUEPRINTS


def test_checkpoint_full_contains_baseline_minted_audit():
    checkpoint = checkpoint_full("KhBSyGEVU8SkMWIXmN8qkrLNwYk1")

    assert checkpoint["user"]["phone_number"] == "+919000000004"
    assert checkpoint["user"]["kyc_completed"] is True
    assert checkpoint["land_parcels"][0]["survey_number"] == "DEMO-004-47"
    assert checkpoint["carbon_audits"][0]["status"] == "MINTED"
    assert checkpoint["carbon_audits"][0]["audit_year"] == 2024


def test_account_four_blueprint_is_resettable():
    assert DEMO_ACCOUNT_BLUEPRINTS["+919000000004"]["persistent"] is False

