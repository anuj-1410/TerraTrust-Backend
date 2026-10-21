"""Supabase-maintained inputs; immutable history and identifiers are automatic.

Only administrators with database access edit these tables. HTTP callers cannot
change the configuration. Snapshot materialization is transactional and bounded
by an advisory lock, and never modifies a previously accepted calculation.
"""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import text

from app.institution_errors import fail
from models.institution.common import digest, normalize
from services.institution.factor_service import validate_config


def identity(value):
    return str(uuid5(NAMESPACE_URL, "terratrust:configuration:" + value))


async def next_version(repo, table, logical_key):
    # Caller holds the configuration/source-family transaction lock.
    if table not in {"carbon_methodologies", "emission_factors", "knowledge_documents"}:
        raise ValueError("Unsupported configuration registry.")
    row = await repo.one(
        f"SELECT COALESCE(MAX(version),0)+1 AS version FROM {table} WHERE logical_key=:key",
        {"key": logical_key},
    )
    return row["version"]


def metadata(label, checksum, timestamp):
    return {
        "title": label,
        "publisher": "Supabase administrator",
        "source_version": checksum[:16],
        "source_url": None,
        "page": None,
        "section": None,
        "published_date": None,
        "retrieved_at": normalize(timestamp),
        "evidence_id": None,
        "checksum": checksum,
        "license": "PROJECT_CONFIGURATION",
        "applicability_notes": "Administrator-maintained calculation configuration; checksum identifies configuration, not a PDF.",
    }


def configuration(settings_row, factors):
    """Resolve human-readable parameter names and freeze the exact DB values."""
    if not settings_row["config"]:
        fail(
            503,
            "CALCULATION_SETTINGS_REQUIRED",
            "Fill institution_calculation_settings.config following INSTITUTION_MANUAL_WORK.md.",
        )
    from services.institution.calculation_defaults import merge

    config = merge(settings_row["config"])
    checksum = digest(
        {
            "settings": config,
            "factors": [
                {
                    key: normalize(row.get(key))
                    for key in (
                        "logical_key",
                        "value",
                        "config",
                        "source_label",
                        "geography_codes",
                        "valid_from",
                        "valid_to",
                    )
                }
                for row in sorted(factors, key=lambda item: item["logical_key"])
            ],
        }
    )
    refs = {}
    for parameter in config.get("parameters", []):
        key = parameter["logical_key"]
        if key in refs:
            fail(
                422,
                "DUPLICATE_PARAMETER",
                "Calculation parameter logical names must be unique.",
            )
        pid = identity(checksum + ":parameter:" + key)
        refs[key] = pid
        if parameter.get("id"):
            refs[str(parameter["id"])] = pid
        parameter["id"] = pid
        parameter["source_metadata"] = metadata(
            "Calculation parameter: " + key, checksum, settings_row["updated_at"]
        )

    def rewrite(node, field=""):
        if isinstance(node, dict):
            return {key: rewrite(value, key) for key, value in node.items()}
        if isinstance(node, list):
            return [rewrite(value, field) for value in node]
        return (
            refs.get(str(node), node)
            if field.endswith("_ids") or field.endswith("_id")
            else node
        )

    config = rewrite(config)
    config["quality_policy"]["measurement_protocol_version"] = identity(
        checksum + ":measurement"
    )
    config["quality_policy"]["uncertainty"]["method_version"] = identity(
        checksum + ":method"
    )
    source = metadata(
        "Institution calculation settings", checksum, settings_row["updated_at"]
    )
    for key in (
        "quality_policy",
        "uncertainty_policy",
        "attribution_policy",
        "ranking_config",
    ):
        config[key]["source_metadata"] = source
    if (
        not config["attribution_policy"]["supported_gain_rules"]
        and not config["attribution_policy"]["supported_loss_rules"]
    ):
        config["attribution_policy"]["validation_artifact_id"] = None
    mappings = config.setdefault("factor_mappings", [])
    # Existing explicit published factor mappings may remain in an imported base
    # method. Editable rows replace the matching category/subcategory/purpose.
    factor_rows = []
    for row in factors:
        value = Decimal(str(row["value"]))
        if not value.is_finite() or value < 0:
            fail(
                422,
                "INVALID_FACTOR_VALUE",
                "Formula values must be finite and nonnegative.",
            )
        fc = normalize(
            validate_config("FACTOR", {**row["config"], "value": str(value)})
        )
        fid = identity(checksum + ":factor:" + row["logical_key"])
        factor_rows.append((fid, row, fc))
    grouped = {}
    for fid, row, fc in factor_rows:
        key = (fc["category"], fc["subcategory"], fc["process"], fc["scope"])
        grouped.setdefault(key, []).append(fid)
    for (category, subcategory, process, scope), ids in grouped.items():
        mappings = [
            mapping
            for mapping in mappings
            if (
                mapping["category"],
                mapping["subcategory"],
                mapping["process"],
                mapping["scope"],
            )
            != (category, subcategory, process, scope)
        ]
        mappings.append(
            {
                "category": category,
                "subcategory": subcategory,
                "process": process,
                "scope": scope,
                "allowed_factor_ids": ids,
            }
        )
    config["factor_mappings"] = mappings
    config = normalize(validate_config("METHOD", config))
    # Tree calibration uses the same species constants as the farmer engine.
    # Editable accounting values must not silently change the measurement check.
    from services.fusion_engine import SPECIES_WOOD_DENSITY, normalise_species_name

    for parameter in config["parameters"]:
        if parameter["kind"] == "WOOD_DENSITY":
            species_rule = (
                parameter["applicability"].get("properties", {}).get("species_code", {})
            )
            species = species_rule.get("const")
            if species:
                canonical = normalise_species_name(species)
                rho = Decimal(str(parameter["value"]))
                if parameter["unit"] == "kg/m3":
                    rho /= 1000
                if rho != Decimal(str(SPECIES_WOOD_DENSITY[canonical])):
                    fail(
                        422,
                        "BIOMASS_PARAMETER_MISMATCH",
                        "Wood densities must match the shared farmer implementation.",
                    )
    return checksum, config, factor_rows


async def current_method(repo, actor, *, required=True):
    await repo.conn.execute(
        text(
            "SELECT pg_advisory_xact_lock(hashtextextended('tt:calculation-configuration',0))"
        )
    )
    await repo.conn.execute(
        text(
            "LOCK TABLE institution_calculation_settings,institution_formula_values IN SHARE MODE"
        )
    )
    row = await repo.one(
        "SELECT * FROM institution_calculation_settings WHERE logical_key='CURRENT' FOR SHARE",
        required=False,
    )
    if not row or not row["config"]:
        # Existing installations can keep their actual published configuration.
        existing = await repo.one(
            "SELECT * FROM carbon_methodologies WHERE status='APPROVED' AND data_origin='LIVE' ORDER BY validated_at DESC NULLS LAST,created_at DESC,id DESC LIMIT 1",
            required=False,
        )
        if existing or not required:
            return existing
        fail(
            503,
            "CALCULATION_SETTINGS_REQUIRED",
            "Fill institution_calculation_settings.config following INSTITUTION_MANUAL_WORK.md.",
        )
    factors = await repo.find("institution_formula_values", {"enabled": True})
    try:
        checksum, config, factors = configuration(row, factors)
    except (ValueError, TypeError, KeyError, ArithmeticError):
        fail(
            503,
            "CALCULATION_SETTINGS_INVALID",
            "Check the required values in institution_calculation_settings and institution_formula_values.",
        )
    mid = identity(checksum + ":method")
    prior = await repo.get("carbon_methodologies", mid, required=False)
    if prior:
        if prior["status"] != "APPROVED":
            fail(
                409,
                "METHOD_NOT_AVAILABLE",
                "The current calculation configuration was deactivated.",
            )
        return prior
    now = datetime.now(timezone.utc)

    def base(key, title, cfg):
        return {
            "logical_key": key,
            "version": row["input_revision"],
            "schema_version": 1,
            "title": title,
            "publisher": "Supabase administrator",
            "source_version": "DATABASE_CONFIGURATION:" + checksum,
            "license": "PROJECT_CONFIGURATION",
            "source_evidence_id": None,
            "geography_codes": [],
            "applicability_notes": "Administrator-maintained database values; no source PDF or independent review is asserted.",
            "config": cfg,
            "status": "DRAFT",
            "artifact_hash": digest(cfg),
            "author_id": actor["id"],
            "validated_at": now,
        }

    for fid, factor, fc in factors:
        key = "DB:" + factor["logical_key"]
        await repo.insert(
            "emission_factors",
            {
                **base(key, factor["source_label"], fc),
                "version": await next_version(repo, "emission_factors", key),
                "id": fid,
                "status": "APPROVED",
                "geography_codes": factor["geography_codes"],
                "valid_from": factor["valid_from"],
                "valid_to": factor["valid_to"],
            },
        )
    version = await next_version(repo, "carbon_methodologies", "DB:CURRENT")
    await repo.insert(
        "carbon_methodologies",
        {
            **base("DB:CURRENT", "Institution calculation configuration", config),
            "id": mid,
            "version": version,
        },
    )
    for parameter in config["parameters"]:
        await repo.insert("method_parameters", {"methodology_id": mid, **parameter})
    quality = config["quality_policy"]
    await repo.insert(
        "method_quality_policies",
        {
            "id": identity(checksum + ":quality"),
            "methodology_id": mid,
            "version": version,
            "config": quality,
            "artifact_hash": digest(quality),
        },
    )
    method = await repo.update("carbon_methodologies", mid, {"status": "APPROVED"})
    await repo.event(
        actor,
        "DATABASE_CALCULATION_SETTINGS_SNAPSHOTTED",
        "carbon_methodologies",
        method,
        details={
            "settings_id": str(row["id"]),
            "settings_revision": row["input_revision"],
        },
    )
    return method
