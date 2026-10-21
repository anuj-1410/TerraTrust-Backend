"""Validate the deployable starter settings without contacting any database."""

from datetime import datetime, timezone
from decimal import Decimal

from pglast import parse_sql

from scripts.institution.normal_setup_sql import factor_rows, sql
from services.institution.calculation_defaults import normal_settings
from services.institution.runtime_configuration import configuration


def test_starter_settings_and_every_enabled_factor_pass_runtime_validation():
    rows = [row for row in factor_rows() if row["enabled"]]
    _, method, resolved = configuration(
        {"config": normal_settings(), "updated_at": datetime.now(timezone.utc)}, rows
    )
    assert len(resolved) == 28
    assert method["campus_processing"] is None
    assert method["quality_policy"]["evidence"]["minimum_photos"] == 1
    assert method["epsilon_tCO2e"] == "0.01"
    diesel = next(
        fc
        for _, row, fc in resolved
        if row["logical_key"] == "TT_STARTER_STATIONARY_DIESEL_GENERATOR"
    )
    assert Decimal(diesel["value"]) * Decimal("100") / 1000 == Decimal("0.268")
    assert diesel["numerator_unit"] == "kgCO2"
    assert "CH4" in diesel["exclusions"] and "N2O" in diesel["exclusions"]


def test_sql_is_valid_and_preserves_site_configuration_and_historical_dates():
    statement = sql(normal_settings())
    assert len(parse_sql(statement)) == 5
    assert '"campus_processing"' not in statement
    assert "ON CONFLICT(logical_key) DO UPDATE" in statement
    assert "public.users" not in statement
    rows = {row["logical_key"]: row for row in factor_rows()}
    pending = rows["TT_STARTER_GRID_CURRENT_PENDING"]
    assert pending["enabled"] is False and pending["value"] is None
    historical = rows["TT_STARTER_GRID_FY2024_25_CEA21"]
    assert (historical["valid_from"], historical["valid_to"]) == (
        "2024-04-01",
        "2025-03-31",
    )
    assert "NOT the current" in historical["source_label"]
