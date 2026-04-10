import os
import sys
import types

os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")

ee_stub = types.ModuleType("ee")
ee_stub.Image = object
ee_stub.ImageCollection = object
ee_stub.Feature = object
ee_stub.FeatureCollection = object
ee_stub.Geometry = types.SimpleNamespace(Point=lambda *_args, **_kwargs: None)
ee_stub.Classifier = types.SimpleNamespace(smileGradientTreeBoost=lambda *_args, **_kwargs: None)
ee_stub.Terrain = types.SimpleNamespace(slope=lambda *_args, **_kwargs: None)
ee_stub.Number = lambda *_args, **_kwargs: types.SimpleNamespace(getInfo=lambda: 1)
ee_stub.ServiceAccountCredentials = lambda *_args, **_kwargs: None
ee_stub.Initialize = lambda *_args, **_kwargs: None
ee_stub.Filter = types.SimpleNamespace(
    eq=lambda *_args, **_kwargs: None,
    listContains=lambda *_args, **_kwargs: None,
    lt=lambda *_args, **_kwargs: None,
)
sys.modules.setdefault("ee", ee_stub)

database_stub = types.ModuleType("app.database")
database_stub.supabase_client = None
sys.modules.setdefault("app.database", database_stub)

config_stub = types.ModuleType("app.config")
config_stub.settings = types.SimpleNamespace(
    GOOGLE_APPLICATION_CREDENTIALS="",
    GOOGLE_CLOUD_PROJECT="test-project",
)
sys.modules.setdefault("app.config", config_stub)

import pytest

from services import fusion_engine


class _SupabaseQueryStub:
    def __init__(self, data):
        self._data = data

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def lt(self, *_args, **_kwargs):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def execute(self):
        class _Response:
            data = self._data

        return _Response()


class _SupabaseStub:
    def __init__(self, data):
        self._data = data

    def table(self, _table_name):
        return _SupabaseQueryStub(self._data)


def test_calculate_credits_returns_zero_when_biomass_does_not_grow(monkeypatch):
    monkeypatch.setattr(
        fusion_engine,
        "supabase_client",
        _SupabaseStub([{"total_biomass_tonnes": 14.8, "status": "MINTED"}]),
    )

    result = fusion_engine.calculate_credits(14.8, "land-1", 2026)

    assert result["credits_issued"] == 0
    assert result["delta_biomass"] == 0
    assert result["prev_year_biomass"] == 14.8


def test_calculate_credits_converts_growth_to_co2_equivalent(monkeypatch):
    monkeypatch.setattr(
        fusion_engine,
        "supabase_client",
        _SupabaseStub([{"total_biomass_tonnes": 10.0, "status": "MINTED"}]),
    )

    result = fusion_engine.calculate_credits(12.0, "land-1", 2026)

    assert result["delta_biomass"] == 2.0
    assert result["carbon_tonnes"] == pytest.approx(0.94)
    assert result["co2_equivalent"] == pytest.approx(3.4470)
    assert result["credits_issued"] == pytest.approx(3.4470)


def test_calculate_credits_uses_zero_credit_baseline_when_no_previous_audit(monkeypatch):
    monkeypatch.setattr(
        fusion_engine,
        "supabase_client",
        _SupabaseStub([]),
    )

    result = fusion_engine.calculate_credits(12.0, "land-1", 2026)

    assert result["credits_issued"] == 0
    assert result["delta_biomass"] == 0
    assert result["reason"] == "Baseline year established; future growth earns credits."


def test_normalise_species_name_accepts_scientific_aliases():
    assert fusion_engine.normalise_species_name("Dalbergia sissoo") == "Indian Rosewood"
