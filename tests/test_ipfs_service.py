import os
import sys
import types

os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")

config_stub = types.ModuleType("app.config")
config_stub.settings = types.SimpleNamespace(
    PINATA_JWT="",
    PINATA_GATEWAY_URL="",
)
sys.modules["app.config"] = config_stub
sys.modules.setdefault("httpx", types.ModuleType("httpx"))
sys.modules.pop("services.ipfs_service", None)

from services import ipfs_service


def test_to_gateway_url_supports_bare_cid(monkeypatch):
    monkeypatch.setattr(
        ipfs_service.settings,
        "PINATA_GATEWAY_URL",
        "gateway.pinata.cloud",
        raising=False,
    )

    assert (
        ipfs_service.to_gateway_url("bafybeigdyrzt3examplecid")
        == "https://gateway.pinata.cloud/ipfs/bafybeigdyrzt3examplecid"
    )


def test_to_gateway_url_normalises_prefixed_forms(monkeypatch):
    monkeypatch.setattr(
        ipfs_service.settings,
        "PINATA_GATEWAY_URL",
        "gateway.pinata.cloud",
        raising=False,
    )

    assert (
        ipfs_service.to_gateway_url("ipfs://bafybeiexample")
        == "https://gateway.pinata.cloud/ipfs/bafybeiexample"
    )
    assert (
        ipfs_service.to_gateway_url("/ipfs/bafybeiexample")
        == "https://gateway.pinata.cloud/ipfs/bafybeiexample"
    )
    assert (
        ipfs_service.to_gateway_url("ipfs/bafybeiexample")
        == "https://gateway.pinata.cloud/ipfs/bafybeiexample"
    )