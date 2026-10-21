"""Institution inputs/results around the exact existing farmer training function."""

import asyncio
import math
from datetime import date, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from app.config import settings
from app.institution_errors import fail
from models.institution.common import digest, normalize

ENGINE = "FARMER_GEE_GBT_V3_1"
MODEL_ID = str(uuid5(NAMESPACE_URL, "terratrust:" + ENGINE))
FEATURE_ID = str(uuid5(NAMESPACE_URL, "terratrust:" + ENGINE + ":dynamic-sensors"))


def choice():
    return {
        "id": MODEL_ID,
        "feature_schema_version": FEATURE_ID,
        "mode": "SHARED_FARMER_RUNTIME_TRAINING",
        "domain": {
            "type": "object",
            "properties": {"domain": {"const": "INSTITUTION"}},
            "required": ["domain"],
            "additionalProperties": False,
        },
        "required_sensors": ["SENTINEL_1", "SENTINEL_2", "SRTM"],
        "quality_summary": "Trains the existing farmer GEE gradient-tree-boosting implementation on this survey; uses its NISAR/GEDI availability fallbacks.",
    }


async def model(repo, actor):
    existing = await repo.get("model_registry", MODEL_ID, required=False)
    if existing:
        if existing["status"] != "ACTIVE" or existing["config"].get("engine") != ENGINE:
            fail(
                409,
                "MODEL_NOT_AVAILABLE",
                "The shared training implementation is unavailable.",
            )
        return existing
    from sqlalchemy import text

    await repo.conn.execute(
        text(
            "SELECT pg_advisory_xact_lock(hashtextextended('tt:runtime-fusion-model',0))"
        )
    )
    existing = await repo.get("model_registry", MODEL_ID, required=False)
    if existing:
        if existing["status"] != "ACTIVE" or existing["config"].get("engine") != ENGINE:
            fail(
                409,
                "MODEL_NOT_AVAILABLE",
                "The shared training implementation is unavailable.",
            )
        return existing
    config = {
        "engine": ENGINE,
        "implementation": "services.fusion_engine.run_fusion",
        "feature_schema_version": FEATURE_ID,
        "training": "PER_RUN",
        "number_of_trees": 100,
        "shrinkage": "0.05",
        "sampling_rate": "0.7",
        "pixel_area_ha": "0.01",
        "carbon_fraction": "0.47",
        "co2_multiplier": "3.667",
    }
    return await repo.insert(
        "model_registry",
        {
            "id": MODEL_ID,
            "logical_key": ENGINE,
            "version": 1,
            "schema_version": 1,
            "title": "Shared farmer runtime biomass training",
            "publisher": "TerraTrust",
            "source_version": ENGINE,
            "license": "PROJECT_IMPLEMENTATION",
            "source_evidence_id": None,
            "geography_codes": [],
            "applicability_notes": choice()["quality_summary"],
            "config": config,
            "status": "ACTIVE",
            "artifact_hash": digest(config),
            "author_id": actor["id"],
        },
    )


def validate(frozen):
    if (
        str(frozen["model"]["id"]) != MODEL_ID
        or frozen["model"]["config"].get("engine") != ENGINE
    ):
        fail(
            409,
            "MODEL_RUNTIME_MISMATCH",
            "Refresh the institution assessment to select the shared farmer implementation.",
        )
    request = frozen["request"]
    start, end, assessed = (
        date.fromisoformat(request[key])
        for key in ("observation_start", "observation_end", "assessment_date")
    )
    if start > end or end > assessed:
        fail(
            422,
            "INVALID_OBSERVATION_WINDOW",
            "Observation dates must be ordered and cannot follow assessment.",
        )
    temporal = frozen["survey"]["policy"]["config"].get("temporal", {})
    if (
        temporal.get("max_window_days") is not None
        and (end - start).days > temporal["max_window_days"]
    ):
        fail(
            422,
            "OBSERVATION_WINDOW_TOO_LARGE",
            "The selected observation window exceeds the survey configuration.",
        )
    return {
        "domain": "INSTITUTION",
        "engine": ENGINE,
        "training": "CURRENT_ACCEPTED_SURVEY",
    }


def scans(frozen):
    from services.fusion_engine import normalise_species_name
    from services.shared.allometry_service import dbh

    output = []
    for row in frozen["survey"]["measurements"]:
        raw = row["raw_input"]
        try:
            species = normalise_species_name(raw["species_code"])
            location = raw["location"]
            if row["validation_status"] != "VALID" or location["type"] != "Point":
                raise ValueError()
            lng, lat = map(float, location["coordinates"])
            diameter, height = float(dbh(raw)), float(raw["height_m"])
            if (
                not all(map(math.isfinite, (lng, lat, diameter, height)))
                or not -180 <= lng <= 180
                or not -90 <= lat <= 90
                or min(diameter, height) <= 0
            ):
                raise ValueError()
        except (ValueError, TypeError, KeyError, ArithmeticError):
            fail(
                422,
                "SURVEY_NEEDS_DATA",
                "Shared fusion requires supported species, real GPS and positive DBH/height measurements.",
            )
        output.append(
            {
                "id": str(row["id"]),
                "species": species,
                "dbh_cm": diameter,
                "height_m": height,
                "gps": {"lat": lat, "lng": lng},
            }
        )
    if len(output) < 9:
        fail(
            422,
            "INSUFFICIENT_SAMPLES",
            "The existing farmer training code requires at least nine eligible real tree samples.",
        )
    return output


def run(frozen):
    from services.fusion_engine import run_fusion

    validate(frozen)
    source = {
        "asset_id": settings.NISAR_GEE_ASSET_ID.strip(),
        "asset_type": settings.NISAR_GEE_ASSET_TYPE,
        "hh_band": settings.NISAR_HH_BAND,
        "hv_band": settings.NISAR_HV_BAND,
    }
    if source != frozen.get("nisar_source"):
        fail(
            409,
            "SATELLITE_CONFIGURATION_CHANGED",
            "NISAR settings changed after acceptance; submit a new institution assessment.",
        )
    request = frozen["request"]
    try:
        result = run_fusion(
            str(request["survey_id"]),
            str(frozen["campus"]["id"]),
            scans(frozen),
            frozen["mask"]["eligible_geometry"],
            int(request["assessment_date"][:4]),
            observation_window=(
                request["observation_start"],
                (
                    date.fromisoformat(request["observation_end"]) + timedelta(days=1)
                ).isoformat(),
            ),
        )
    except ValueError:
        fail(
            422,
            "INSUFFICIENT_SAMPLES",
            "Shared fusion did not find nine usable real tree/satellite training samples.",
        )
    amount = float(result["total_biomass_tonnes"])
    if not math.isfinite(amount) or amount < 0:
        fail(
            422,
            "MODEL_OUTPUT_INVALID",
            "The shared training function returned invalid biomass.",
        )
    return result


async def compute(repo, frozen):
    from services.shared.carbon_stock_service import stock

    output = await asyncio.to_thread(run, frozen)
    result = stock(
        Decimal(str(output["total_biomass_tonnes"])),
        Decimal("0.47"),
        frozen,
        co2_multiplier=Decimal("3.667"),
    )
    result["coverage"] = {
        "eligible_area_ha": frozen["mask"]["eligible_area_ha"],
        "covered_area_ha": None,
        "coverage_ratio": None,
        "aggregation": "EXISTING_FARMER_10M_GRID",
    }
    result["provenance"] = {
        "domain": "INSTITUTION",
        "algorithm": ENGINE,
        "implementation": "services.fusion_engine.run_fusion",
        "training_points_count": output["training_points_count"],
        "tree_measurements": normalize(output["tree_measurements"]),
        "satellite_features": normalize(output["satellite_features"]),
        "nisar_source": frozen["nisar_source"],
        "carbon_fraction": "0.47",
        "co2_multiplier": "3.667",
        "quality_policy_version": str(frozen["survey"]["policy"]["id"]),
        "pools": frozen["method"]["config"]["pools"],
        "plot_set_id": str(frozen["survey"]["survey"]["plot_set_id"]),
        "quality_policy_hash": frozen["survey"]["policy"]["artifact_hash"],
        "observation_window": {
            key: frozen["request"][key]
            for key in ("observation_start", "observation_end")
        },
        "tree_ids": sorted(str(tree["id"]) for tree in frozen["survey"]["trees"]),
    }
    result["quality_flags"].append("PIXEL_COVERAGE_NOT_QUANTIFIED")
    summary = {
        "type": "Feature",
        "geometry": frozen["mask"]["eligible_geometry"],
        "properties": {
            "agb_t": result["agb_t"],
            "engine": ENGINE,
            "training_points_count": output["training_points_count"],
            "description": "Biomass summary for the eligible geometry; no per-pixel map is supplied by the shared farmer function.",
        },
    }
    return normalize(result), summary
