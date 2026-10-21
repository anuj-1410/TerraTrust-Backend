"""Shared implementation, editable inputs and real local authenticated encryption."""

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.config import settings
from app.institution_errors import InstitutionError
from services.institution import farmer_biomass
from services.institution.calculation_defaults import template
from services.institution.runtime_configuration import configuration, current_method
from services.shared import receipt_crypto_service as crypto


def site_config():
    """Synthetic test-site limits, never deployed as scientific defaults."""
    config = template()
    q = config["quality_policy"]
    q["sampling"].update(
        plot_area_ha="0.01", min_spacing_m="5", required_plots_by_stratum={"TEST": 3}
    )
    q["dimensions"].update(
        dbh_min_cm="1",
        dbh_max_cm="200",
        height_min_m="1",
        height_max_m="100",
        reference_height_m="1.3",
    )
    q["gps"].update(max_accuracy_m="10", boundary_tolerance_m="0", plot_tolerance_m="0")
    q["evidence"]["minimum_photos"] = 1
    q["temporal"].update(max_window_days=366, seasonal_tolerance_days=15)
    q["validation"].update(
        minimum_eligible_area_ha="0.01", minimum_coverage_ratio="0.5"
    )
    config["campus_processing"] = None
    return config


def editable_row():
    return {
        "id": str(uuid4()),
        "config": site_config(),
        "input_revision": 1,
        "updated_at": datetime.now(timezone.utc),
    }


def factor_row(value="0.1"):
    return {
        "id": str(uuid4()),
        "logical_key": "TEST_GRID",
        "value": value,
        "source_label": "Synthetic test coefficient",
        "geography_codes": ["TEST"],
        "valid_from": None,
        "valid_to": None,
        "enabled": True,
        "config": {
            "category": "ELECTRICITY",
            "subcategory": "GRID",
            "process": "GRID",
            "fuel": None,
            "material": None,
            "numerator_unit": "kgCO2e",
            "denominator_unit": "kWh",
            "gas_basis": "COMBINED_CO2E",
            "scope": "SCOPE_2",
            "purpose": "INVENTORY",
            "boundary": "Synthetic test",
            "exclusions": [],
            "mass_basis": None,
            "standard_conditions": None,
            "gwp": None,
            "uncertainty": None,
        },
    }


def frozen_survey():
    measurement = {
        "id": str(uuid4()),
        "validation_status": "VALID",
        "raw_input": {
            "species_code": "Neem",
            "measurement_form": "DIAMETER",
            "dbh_cm": "20",
            "height_m": "8",
            "location": {"type": "Point", "coordinates": [73.8, 18.5]},
        },
    }
    return {
        "request": {
            "survey_id": str(uuid4()),
            "assessment_date": "2025-12-31",
            "observation_start": "2025-01-01",
            "observation_end": "2025-12-31",
        },
        "campus": {"id": str(uuid4())},
        "mask": {
            "eligible_geometry": {
                "type": "Polygon",
                "coordinates": [[[73, 18], [74, 18], [74, 19], [73, 18]]],
            },
            "eligible_area_ha": "5",
        },
        "model": {
            "id": farmer_biomass.MODEL_ID,
            "config": {"engine": farmer_biomass.ENGINE},
        },
        "method": {"config": {"pools": ["LIVE_ABOVE_GROUND"]}},
        "survey": {
            "policy": {
                "id": str(uuid4()),
                "config": {"temporal": {"max_window_days": 366}},
                "artifact_hash": "a" * 64,
            },
            "survey": {"plot_set_id": str(uuid4())},
            "measurements": [
                {**deepcopy(measurement), "id": str(uuid4())} for _ in range(9)
            ],
            "trees": [],
        },
        "nisar_source": {
            "asset_id": settings.NISAR_GEE_ASSET_ID.strip(),
            "asset_type": settings.NISAR_GEE_ASSET_TYPE,
            "hh_band": settings.NISAR_HH_BAND,
            "hv_band": settings.NISAR_HV_BAND,
        },
        "baseline": None,
    }


def test_institution_calls_actual_farmer_entrypoint_with_own_inputs(monkeypatch):
    import services.fusion_engine as farmer

    frozen = frozen_survey()
    calls = []

    def shared(*args, **kwargs):
        calls.append((args, kwargs))
        return {"total_biomass_tonnes": 123.4567}

    monkeypatch.setattr(farmer, "run_fusion", shared)
    assert farmer_biomass.run(frozen)["total_biomass_tonnes"] == 123.4567
    args, kwargs = calls[0]
    assert args[0:2] == (frozen["request"]["survey_id"], frozen["campus"]["id"])
    assert args[2] == farmer_biomass.scans(frozen)
    assert args[3] == frozen["mask"]["eligible_geometry"]
    assert kwargs == {"observation_window": ("2025-01-01", "2026-01-01")}


@pytest.mark.parametrize(
    "problem",
    ["count", "species", "gps", "diameter", "height", "invalid", "nisar_drift"],
)
def test_shared_adapter_rejects_missing_real_inputs_before_provider(problem):
    frozen = frozen_survey()
    raw = frozen["survey"]["measurements"][0]["raw_input"]
    if problem == "count":
        frozen["survey"]["measurements"].pop()
    elif problem == "species":
        raw["species_code"] = "Unknown"
    elif problem == "gps":
        raw["location"]["coordinates"] = [999, 18]
    elif problem == "diameter":
        raw["dbh_cm"] = "NaN"
    elif problem == "height":
        raw["height_m"] = None
    elif problem == "invalid":
        frozen["survey"]["measurements"][0]["validation_status"] = "NEEDS_DATA"
    else:
        frozen["nisar_source"]["asset_id"] = "changed/source"
    with pytest.raises(InstitutionError):
        if problem == "nisar_drift":
            farmer_biomass.run(frozen)
        else:
            farmer_biomass.scans(frozen)


@pytest.mark.asyncio
async def test_shared_stock_uses_same_carbon_conversion_and_honest_coverage(
    monkeypatch,
):
    frozen = frozen_survey()

    def result(_):
        return {
            "total_biomass_tonnes": "100",
            "training_points_count": 9,
            "tree_measurements": [],
            "satellite_features": {"radar_source": "S1"},
        }

    monkeypatch.setattr(farmer_biomass, "run", result)
    output, geometry = await farmer_biomass.compute(None, frozen)
    assert Decimal(output["stock_tC"]) == Decimal(47)
    assert Decimal(output["stock_co2_t"]) == Decimal(100) * Decimal("0.47") * Decimal(
        "3.667"
    )
    assert output["removal_tCO2"] is None
    assert output["coverage"]["coverage_ratio"] is None
    assert "PIXEL_COVERAGE_NOT_QUANTIFIED" in output["quality_flags"]
    assert geometry["geometry"] == frozen["mask"]["eligible_geometry"]


def test_editable_settings_generate_ids_sources_and_change_with_values():
    row, factor = editable_row(), factor_row()
    first, config, factors = configuration(row, [factor])
    assert all(
        parameter["source_metadata"]["evidence_id"] is None
        for parameter in config["parameters"]
    )
    assert config["quality_policy"]["eligibility"]["allometry_ids"] == [
        config["parameters"][0]["id"]
    ]
    assert factors[0][2]["value"] == "0.1"
    assert config["factor_mappings"][0]["allowed_factor_ids"] == [factors[0][0]]
    assert configuration(row, [factor])[0] == first
    changed = {**factor, "value": "0.2"}
    second, next_config, next_factors = configuration(row, [changed])
    assert first != second and factors[0][0] != next_factors[0][0]
    assert config["parameters"][0]["id"] != next_config["parameters"][0]["id"]
    assert factor["value"] == "0.1"  # No mutation of input/history.


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1"])
def test_invalid_editable_coefficients_are_rejected(value):
    with pytest.raises(InstitutionError):
        configuration(editable_row(), [factor_row(value)])


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("gps", "boundary_tolerance_m", "-1"),
        ("dimensions", "reference_height_m", "0"),
        ("evidence", "minimum_photos", -1),
        ("species", "minimum_classifier_confidence", "1.1"),
    ],
)
def test_invalid_site_limits_are_rejected(section, key, value):
    row = editable_row()
    row["config"]["quality_policy"][section][key] = value
    with pytest.raises(InstitutionError):
        configuration(row, [])


def test_template_does_not_invent_real_site_configuration():
    config = template()
    assert config["quality_policy"]["gps"]["max_accuracy_m"] is None
    assert config["campus_processing"]["mask_asset_id"] is None
    with pytest.raises(InstitutionError):
        configuration({**editable_row(), "config": config}, [])


def test_shared_settings_cannot_enter_a_resettable_demo_manifest():
    from demo.institution.manifest import ALLOWED

    assert (
        not {
            "institution_calculation_settings",
            "institution_formula_values",
            "institution_knowledge_sources",
        }
        & ALLOWED
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,key",
    [
        ("METHOD", "DB:CURRENT"),
        ("MODEL", farmer_biomass.ENGINE),
        ("KNOWLEDGE", "DB:guidance"),
    ],
)
async def test_manual_registry_api_cannot_occupy_automatic_registry_families(kind, key):
    from services.institution.registry_service import create

    with pytest.raises(InstitutionError) as error:
        await create(None, {"kind": kind, "logical_key": key}, kind)
    assert error.value.code == "SERVER_OWNED_REGISTRY_KEY"


class MemoryRepo:
    def __init__(self, row=None, factors=()):
        self.tables = {
            "institution_calculation_settings": [row] if row else [],
            "institution_formula_values": list(factors),
        }
        self.conn = self
        self.sql = []

    async def execute(self, sql, params=None):
        self.sql.append((str(sql), params))

    async def one(self, sql, params=None, **kwargs):
        if "SELECT * FROM knowledge_corpus_versions" in sql:
            rows = [
                row
                for row in self.tables.get("knowledge_corpus_versions", [])
                if row["status"] == "ACTIVE"
                and row["embedding_config"]["model"] == params["model"]
                and str(row["embedding_config"]["dimension"]) == params["dimension"]
            ]
            rows.sort(key=lambda row: str(row.get("published_at") or ""), reverse=True)
            return rows[0] if rows else None
        if "MAX(version)" in sql:
            table = sql.split(" FROM ")[1].split()[0]
            versions = [
                row["version"]
                for row in self.tables.get(table, [])
                if row["logical_key"] == params["key"]
            ]
            return {"version": max(versions, default=0) + 1}
        table = (
            "institution_calculation_settings"
            if "institution_calculation_settings" in sql
            else "carbon_methodologies"
        )
        return next(iter(self.tables.get(table, [])), None)

    async def rows(self, sql, params=None):
        return []

    async def find(self, table, filters, **kwargs):
        return [
            row
            for row in self.tables.get(table, [])
            if all(row.get(key) == value for key, value in filters.items())
        ]

    async def get(self, table, rid, **kwargs):
        row = next(
            (row for row in self.tables.get(table, []) if str(row["id"]) == str(rid)),
            None,
        )
        if row is None and kwargs.get("required", True):
            raise AssertionError((table, rid))
        return row

    async def insert(self, table, values):
        if table in {
            "carbon_methodologies",
            "emission_factors",
            "knowledge_documents",
            "model_registry",
        }:
            # Mirror the real phase-011 uniqueness constraint at this boundary.
            assert not any(
                (row["logical_key"], row["version"])
                == (values["logical_key"], values["version"])
                for row in self.tables.get(table, [])
            )
        row = {
            "id": str(uuid4()),
            "input_revision": 1,
            "bytes_deleted_at": None,
            "data_origin": "LIVE",
            **deepcopy(values),
        }
        self.tables.setdefault(table, []).append(row)
        return row

    async def update(self, table, rid, values, **kwargs):
        row = await self.get(table, rid)
        row.update(deepcopy(values))
        return row

    async def event(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_runtime_configuration_preserves_previous_method_and_is_idempotent():
    row, factor = editable_row(), factor_row()
    repo, actor = MemoryRepo(row, [factor]), {"id": str(uuid4())}
    first = deepcopy(await current_method(repo, actor))
    assert (await current_method(repo, actor))["id"] == first["id"]
    assert len(repo.tables["carbon_methodologies"]) == 1
    factor["value"] = "0.2"
    second = await current_method(repo, actor)
    assert second["id"] != first["id"]
    assert second["version"] == 2
    assert sorted(row["version"] for row in repo.tables["emission_factors"]) == [1, 2]
    old = await repo.get("carbon_methodologies", first["id"])
    assert old == first
    assert len(repo.tables["method_quality_policies"]) == 2
    assert any("LOCK TABLE" in sql for sql, _ in repo.sql)


@pytest.fixture
def local_key(monkeypatch, tmp_path):
    path = tmp_path / "receipt.key"
    path.write_bytes(base64.urlsafe_b64encode(bytes(range(32))))
    monkeypatch.setattr(crypto, "KEY_FILE", path)
    return path


@pytest.mark.asyncio
async def test_real_receipt_encryption_roundtrip_and_fresh_nonce(local_key):
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    first = await crypto.encrypt_receipt(
        "123456", "institution/invite/test", "a" * 64, expiry
    )
    second = await crypto.encrypt_receipt(
        "123456", "institution/invite/test", "a" * 64, expiry
    )
    assert first["secret_ciphertext"] != second["secret_ciphertext"]
    assert b"123456" not in first["secret_ciphertext"]
    assert (
        await crypto.decrypt_receipt(first, "institution/invite/test", "a" * 64)
        == "123456"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["bytes", "scope", "request", "key", "expiry", "malformed"]
)
async def test_receipts_authenticate_scope_and_expiry(local_key, change):
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    receipt = await crypto.encrypt_receipt("secret", "scope", "a" * 64, expiry)
    scope, request_hash = "scope", "a" * 64
    if change == "bytes":
        receipt["secret_ciphertext"] = receipt["secret_ciphertext"][:-1] + bytes(
            [receipt["secret_ciphertext"][-1] ^ 1]
        )
    elif change == "scope":
        scope = "other"
    elif change == "request":
        request_hash = "b" * 64
    elif change == "key":
        local_key.write_bytes(base64.urlsafe_b64encode(bytes(reversed(range(32)))))
    elif change == "expiry":
        receipt["secret_expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    else:
        receipt["secret_ciphertext"] = b"bad"
    with pytest.raises(InstitutionError):
        await crypto.decrypt_receipt(receipt, scope, request_hash)


def test_key_generator_refuses_overwrite_and_missing_key_fails_closed(
    monkeypatch, tmp_path
):
    from scripts.institution import create_receipt_key

    path = tmp_path / "key" / "receipt.key"
    monkeypatch.setattr(create_receipt_key, "KEY_FILE", path)
    monkeypatch.setattr(crypto, "KEY_FILE", path)
    with pytest.raises(InstitutionError):
        crypto.local_key()
    create_receipt_key.main()
    key = crypto.local_key()
    assert len(key) == 32
    with pytest.raises(SystemExit):
        create_receipt_key.main()
    assert crypto.local_key() == key
    path.write_bytes(b"bad-key")
    with pytest.raises(InstitutionError):
        crypto.local_key()
