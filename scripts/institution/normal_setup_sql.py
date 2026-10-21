"""Generate a local settings update and source-labelled starter factors; never connect."""

import json
from pathlib import Path

NSE = "https://nsearchives.nseindia.com/web/sites/default/files/inline-files/Industry%20Standards%20Note%20on%20BRSR%20with%20Annexure.pdf"
NITI = "https://www.niti.gov.in/sites/default/files/2026-02/Scenarios-Towards-Viksit-Bharat-and-Net-Zero-Sectoral-Insights-Power.pdf"


def factor_rows():
    rows = []
    fuels = {
        "DIESEL": ("2.68", "L"),
        "PETROL": ("2.27", "L"),
        "CNG": ("2.69", "kg"),
        "LPG": ("2.98", "kg"),
    }

    def add(
        key,
        value,
        category,
        subcategory,
        process,
        unit,
        fuel,
        source,
        start=None,
        end=None,
        enabled=True,
    ):
        config = {
            "category": category,
            "subcategory": subcategory,
            "process": process,
            "fuel": fuel,
            "material": None,
            "numerator_unit": "kgCO2",
            "denominator_unit": unit,
            "gas_basis": "GAS_SPECIFIC",
            "scope": "SCOPE_2" if category == "ELECTRICITY" else "SCOPE_1",
            "purpose": "INVENTORY",
            "boundary": "Generation-only grid CO2"
            if category == "ELECTRICITY"
            else "Direct fossil-fuel combustion CO2",
            "exclusions": [
                "CH4",
                "N2O",
                "upstream lifecycle emissions",
                "biogenic CO2",
            ],
            "mass_basis": None,
            "standard_conditions": None,
            "gwp": None,
            "uncertainty": None,
        }
        rows.append(
            {
                "logical_key": key,
                "value": value,
                "config": config,
                "source_label": source,
                "geography_codes": ["IN", "MH", "IN-MH", "Maharashtra"],
                "valid_from": start,
                "valid_to": end,
                "enabled": enabled,
            }
        )

    for fuel, (value, unit) in fuels.items():
        for vehicle in ("BUS", "CAR", "VAN", "TRUCK", "TWO_WHEELER"):
            add(
                "TT_STARTER_TRANSPORT_" + fuel + "_" + vehicle,
                value,
                "TRANSPORT",
                "OWNED_FLEET",
                vehicle,
                unit,
                fuel,
                "NSE-published BRSR industry standard, Annexure I; CO2 only; fossil fuel, not an ethanol/biodiesel blend: "
                + NSE,
            )
    for fuel, processes in {
        "DIESEL": ("GENERATOR", "HEATING"),
        "PETROL": ("GENERATOR",),
        "CNG": ("COOKING", "HEATING"),
        "LPG": ("COOKING", "HEATING"),
    }.items():
        for process in processes:
            value, unit = fuels[fuel]
            add(
                "TT_STARTER_STATIONARY_" + fuel + "_" + process,
                value,
                "STATIONARY_FUEL",
                fuel,
                process,
                unit,
                fuel,
                "NSE-published BRSR industry standard, Annexure I; direct combustion CO2 only: "
                + NSE,
            )
    add(
        "TT_STARTER_GRID_FY2024_25_CEA21",
        "0.710",
        "ELECTRICITY",
        "GRID",
        "GRID",
        "kWh",
        None,
        "HISTORICAL FY2024-25 only, CEA v21 reproduced by NITI Aayog Annexure E; NOT the current CEA v22 factor: "
        + NITI,
        "2024-04-01",
        "2025-03-31",
    )
    add(
        "TT_STARTER_GRID_CURRENT_PENDING",
        None,
        "ELECTRICITY",
        "GRID",
        "GRID",
        "kWh",
        None,
        "DISABLED: CEA v22 listed, but its PDF redirects to login; verify the current numeric value before enabling. https://cea.nic.in/wp-content/uploads/baseline/2026/09/User_Guide__Version_22.0.pdf",
        enabled=False,
    )
    return rows


def sql(settings):
    settings = dict(settings)
    if settings.get("campus_processing") is None:
        settings.pop(
            "campus_processing", None
        )  # Preserve an existing real mask configuration.
    return (
        """-- Run manually in the EXISTING project after migrations 001-016.
-- Updates only CURRENT settings and the named TT_STARTER_ factor rows.
-- Never rewrites accepted/finalized calculation snapshots or farmer tables.
BEGIN;
UPDATE public.institution_calculation_settings
SET config=config || $tt_settings$"""
        + json.dumps(settings, indent=2)
        + """$tt_settings$::jsonb
WHERE logical_key='CURRENT';

INSERT INTO public.institution_formula_values
 (logical_key,value,config,source_label,geography_codes,valid_from,valid_to,enabled)
SELECT logical_key,value,config,source_label,geography_codes,valid_from,valid_to,enabled
FROM jsonb_to_recordset($tt_factors$"""
        + json.dumps(factor_rows(), indent=2)
        + """$tt_factors$::jsonb)
 AS f(logical_key text,value numeric,config jsonb,source_label text,geography_codes text[],valid_from date,valid_to date,enabled boolean)
ON CONFLICT(logical_key) DO UPDATE SET
 value=EXCLUDED.value,config=EXCLUDED.config,source_label=EXCLUDED.source_label,
 geography_codes=EXCLUDED.geography_codes,valid_from=EXCLUDED.valid_from,valid_to=EXCLUDED.valid_to,enabled=EXCLUDED.enabled;
COMMIT;
SELECT logical_key,value,enabled,valid_from,valid_to FROM public.institution_formula_values
WHERE logical_key LIKE 'TT_STARTER_%' ORDER BY logical_key;
"""
    )


def main():
    from services.institution.calculation_defaults import normal_settings

    directory = Path(".institution-private/supabase")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "institution_settings_and_factors.sql"
    path.write_text(sql(normal_settings()), encoding="utf-8", newline="\n")
    print("OFFLINE SQL", path)


if __name__ == "__main__":
    main()
