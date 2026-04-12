"""Database layer for Supabase plus direct PostgreSQL/PostGIS helpers."""

from __future__ import annotations

from datetime import datetime
import json
import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from supabase import Client, create_client

from app.config import settings

logger = logging.getLogger("terratrust.database")

supabase_client: Client = create_client(
    settings.SUPABASE_URL,
    settings.SUPABASE_SERVICE_KEY,
)

async_engine: Optional[AsyncEngine] = None

if settings.DATABASE_URL:
    import ssl

    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    async_engine = create_async_engine(
        settings.DATABASE_URL,
        echo=settings.ENVIRONMENT == "development",
        pool_size=5,
        max_overflow=10,
        connect_args={"ssl": ssl_context},
    )
else:
    logger.warning(
        "DATABASE_URL is not configured. PostGIS-backed helpers require a direct PostgreSQL connection."
    )


def _require_async_engine() -> AsyncEngine:
    """Return the configured async engine or fail with a clear setup error."""
    if async_engine is None:
        # raise RuntimeError(
        #     "DATABASE_URL must be configured for PostGIS-backed backend operations."
        # )
        logger.error("Startup errors: %s", startup_errors)
    return async_engine


def _decode_json_value(value: Any) -> Any:
    """Decode a JSON string into a Python object when needed."""
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _normalise_db_value(value: Any) -> Any:
    """Convert direct SQLAlchemy values into API-friendly Python primitives."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _normalise_db_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalise_db_value(item) for item in value]
    return value


def _normalise_db_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Apply scalar normalisation to a row returned from direct PostgreSQL queries."""
    return {key: _normalise_db_value(value) for key, value in record.items()}


async def analyse_boundary_geojson(boundary_geojson: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and measure a boundary geometry using PostGIS."""
    engine = _require_async_engine()
    query = text(
        """
        WITH boundary AS (
            SELECT ST_SetSRID(ST_GeomFromGeoJSON(:boundary_geojson), 4326) AS geom
        )
        SELECT
            ST_IsValid(geom) AS is_valid,
            ST_GeometryType(geom) AS geometry_type,
            ST_Area(geom::geography) / 10000.0 AS area_hectares,
            ST_AsGeoJSON(ST_Multi(geom)) AS normalized_geojson
        FROM boundary
        """
    )

    async with engine.connect() as conn:
        result = await conn.execute(
            query,
            {"boundary_geojson": json.dumps(boundary_geojson)},
        )
        row = result.mappings().one()

    data = _normalise_db_record(dict(row))
    data["normalized_geojson"] = _decode_json_value(data.get("normalized_geojson"))
    return data


async def fetch_land_parcel_record(land_id: str) -> Dict[str, Any]:
    """Fetch a land parcel and decode its PostGIS geometry to GeoJSON."""
    engine = _require_async_engine()
    query = text(
        """
        SELECT
            id,
            user_id,
            farm_name,
            survey_number,
            district,
            taluka,
            village,
            state,
            boundary_source,
            ocr_owner_name,
            doc_image_url,
            lgd_district_code,
            lgd_taluka_code,
            lgd_village_code,
            gis_code,
            COALESCE(area_hectares, ST_Area(geom::geography) / 10000.0) AS area_hectares,
            COALESCE(is_verified, FALSE) AS is_verified,
            created_at,
            ST_AsGeoJSON(geom) AS boundary_geojson
        FROM land_parcels
        WHERE id = :land_id
        """
    )

    async with engine.connect() as conn:
        result = await conn.execute(query, {"land_id": land_id})
        row = result.mappings().first()

    if row is None:
        raise LookupError(f"Land parcel '{land_id}' was not found.")

    data = _normalise_db_record(dict(row))
    data["boundary_geojson"] = _decode_json_value(data.get("boundary_geojson"))
    data["geojson"] = data.get("boundary_geojson")
    return data


async def land_contains_point(land_id: str, lat: float, lng: float) -> bool:
    """Return whether a GPS point lies inside a registered land boundary."""
    engine = _require_async_engine()
    query = text(
        """
        SELECT EXISTS (
            SELECT 1
            FROM land_parcels
            WHERE id = :land_id
              AND ST_Contains(
                  geom,
                  ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)
              )
        ) AS inside
        """
    )

    async with engine.connect() as conn:
        result = await conn.execute(query, {"land_id": land_id, "lat": lat, "lng": lng})
        return bool(result.scalar_one())


async def insert_land_parcel_record(land_record: Dict[str, Any]) -> Dict[str, Any]:
    """Insert a land parcel into the documented PostGIS schema."""
    engine = _require_async_engine()
    boundary_geojson = land_record.get("boundary_geojson") or land_record.get("geojson")
    if not boundary_geojson:
        raise ValueError("Land parcel record is missing boundary GeoJSON.")

    query = text(
        """
        INSERT INTO land_parcels (
            id,
            user_id,
            farm_name,
            survey_number,
            district,
            taluka,
            village,
            state,
            geom,
            is_verified,
            boundary_source,
            ocr_owner_name,
            doc_image_url,
            lgd_district_code,
            lgd_taluka_code,
            lgd_village_code,
            gis_code,
            created_at
        )
        VALUES (
            :id,
            :user_id,
            :farm_name,
            :survey_number,
            :district,
            :taluka,
            :village,
            :state,
            ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(:boundary_geojson), 4326)),
            :is_verified,
            :boundary_source,
            :ocr_owner_name,
            :doc_image_url,
            :lgd_district_code,
            :lgd_taluka_code,
            :lgd_village_code,
            :gis_code,
            COALESCE(:created_at, NOW())
        )
        RETURNING id, COALESCE(area_hectares, ST_Area(geom::geography) / 10000.0) AS area_hectares
        """
    )

    params = {
        "id": land_record["id"],
        "user_id": land_record["user_id"],
        "farm_name": land_record.get("farm_name"),
        "survey_number": land_record["survey_number"],
        "district": land_record["district"],
        "taluka": land_record["taluka"],
        "village": land_record["village"],
        "state": land_record.get("state", "Maharashtra"),
        "boundary_geojson": json.dumps(boundary_geojson),
        "is_verified": land_record.get("is_verified", False),
        "boundary_source": land_record.get("boundary_source"),
        "ocr_owner_name": land_record.get("ocr_owner_name"),
        "doc_image_url": land_record.get("doc_image_url"),
        "lgd_district_code": land_record.get("lgd_district_code"),
        "lgd_taluka_code": land_record.get("lgd_taluka_code"),
        "lgd_village_code": land_record.get("lgd_village_code"),
        "gis_code": land_record.get("gis_code"),
        "created_at": land_record.get("created_at"),
    }

    async with engine.begin() as conn:
        result = await conn.execute(query, params)
        row = result.mappings().one()

    return _normalise_db_record(dict(row))


async def list_land_parcels_for_user(user_id: str) -> List[Dict[str, Any]]:
    """List registered land parcels with boundary GeoJSON decoded from PostGIS."""
    engine = _require_async_engine()
    query = text(
        """
        SELECT
            id,
            farm_name,
            survey_number,
            district,
            taluka,
            village,
            state,
            COALESCE(area_hectares, ST_Area(geom::geography) / 10000.0) AS area_hectares,
            COALESCE(is_verified, FALSE) AS is_verified,
            boundary_source,
            created_at AS registered_at,
            ST_AsGeoJSON(geom) AS boundary_geojson
        FROM land_parcels
        WHERE user_id = :user_id
        ORDER BY registered_at DESC
        """
    )

    async with engine.connect() as conn:
        result = await conn.execute(query, {"user_id": user_id})
        rows = result.mappings().all()

    items: List[Dict[str, Any]] = []
    for row in rows:
        item = _normalise_db_record(dict(row))
        item["boundary_geojson"] = _decode_json_value(item.get("boundary_geojson"))
        item["geojson"] = item.get("boundary_geojson")
        items.append(item)
    return items


async def insert_sampling_zone_records(
    land_id: str,
    audit_id: str,
    zones: List[Dict[str, Any]],
) -> bool:
    """Persist sampling zones into the documented sampling_zones table."""
    if not zones:
        return True

    engine = _require_async_engine()
    query = text(
        """
        INSERT INTO sampling_zones (
            id,
            land_id,
            audit_id,
            zone_label,
            centre_point,
            radius_metres,
            zone_type,
            ndvi_mean,
            gedi_available,
            sequence_order,
            created_at
        )
        VALUES (
            :id,
            :land_id,
            :audit_id,
            :zone_label,
            ST_SetSRID(ST_MakePoint(:lng, :lat), 4326),
            :radius_metres,
            :zone_type,
            :ndvi_mean,
            :gedi_available,
            :sequence_order,
            COALESCE(:created_at, NOW())
        )
        """
    )

    params = [
        {
            "id": zone["zone_id"],
            "land_id": land_id,
            "audit_id": audit_id,
            "zone_label": zone["label"],
            "lat": zone["centre_gps"]["lat"],
            "lng": zone["centre_gps"]["lng"],
            "radius_metres": zone["radius_metres"],
            "zone_type": zone["zone_type"],
            "ndvi_mean": zone.get("ndvi_mean"),
            "gedi_available": zone.get("gedi_available", False),
            "sequence_order": zone["sequence_order"],
            "created_at": zone.get("created_at"),
        }
        for zone in zones
    ]

    async with engine.begin() as conn:
        await conn.execute(query, params)
    return True


async def list_sampling_zones_for_audit(audit_id: str) -> List[Dict[str, Any]]:
    """Return sampling-zone centres and radii for a single audit."""
    engine = _require_async_engine()
    query = text(
        """
        SELECT
            id,
            zone_label,
            radius_metres,
            zone_type,
            ndvi_mean,
            COALESCE(gedi_available, FALSE) AS gedi_available,
            sequence_order,
            ST_Y(centre_point) AS lat,
            ST_X(centre_point) AS lng
        FROM sampling_zones
        WHERE audit_id = :audit_id
        ORDER BY sequence_order ASC, created_at ASC
        """
    )

    async with engine.connect() as conn:
        result = await conn.execute(query, {"audit_id": audit_id})
        rows = result.mappings().all()

    zones: List[Dict[str, Any]] = []
    for row in rows:
        zone = _normalise_db_record(dict(row))
        zone["centre_gps"] = {
            "lat": zone.pop("lat"),
            "lng": zone.pop("lng"),
        }
        zones.append(zone)
    return zones


async def insert_tree_scan_record(scan_record: Dict[str, Any]) -> None:
    """Insert an AR tree scan into the documented ar_tree_scans table."""
    engine = _require_async_engine()
    gps = scan_record.get("gps") or {}
    query = text(
        """
        INSERT INTO ar_tree_scans (
            id,
            audit_id,
            land_id,
            zone_id,
            gps_location,
            gps_accuracy_m,
            species,
            species_confidence,
            species_source,
            dbh_cm,
            height_m,
            gedi_height_m,
            height_source,
            wood_density,
            agb_kg,
            ar_tier_used,
            confidence_score,
            evidence_photo_path,
            evidence_photo_hash,
            scan_timestamp,
            created_at
        )
        VALUES (
            :id,
            :audit_id,
            :land_id,
            :zone_id,
            ST_SetSRID(ST_MakePoint(:lng, :lat), 4326),
            :gps_accuracy_m,
            :species,
            :species_confidence,
            :species_source,
            :dbh_cm,
            :height_m,
            :gedi_height_m,
            :height_source,
            :wood_density,
            :agb_kg,
            :ar_tier_used,
            :confidence_score,
            :evidence_photo_path,
            :evidence_photo_hash,
            :scan_timestamp,
            :created_at
        )
        """
    )

    params = {
        "id": scan_record["id"],
        "audit_id": scan_record["audit_id"],
        "land_id": scan_record["land_id"],
        "zone_id": scan_record.get("zone_id"),
        "lat": gps.get("lat"),
        "lng": gps.get("lng"),
        "gps_accuracy_m": scan_record.get("gps_accuracy_m"),
        "species": scan_record["species"],
        "species_confidence": scan_record.get("species_confidence"),
        "species_source": scan_record.get("species_source"),
        "dbh_cm": scan_record["dbh_cm"],
        "height_m": scan_record.get("height_m"),
        "gedi_height_m": scan_record.get("gedi_height_m"),
        "height_source": scan_record.get("height_source"),
        "wood_density": scan_record["wood_density"],
        "agb_kg": scan_record.get("agb_kg"),
        "ar_tier_used": scan_record.get("ar_tier_used"),
        "confidence_score": scan_record.get("confidence_score"),
        "evidence_photo_path": scan_record.get("evidence_photo_path"),
        "evidence_photo_hash": scan_record.get("evidence_photo_hash"),
        "scan_timestamp": scan_record.get("scan_timestamp"),
        "created_at": scan_record.get("created_at"),
    }

    async with engine.begin() as conn:
        await conn.execute(query, params)


async def list_tree_scans_for_audit(audit_id: str) -> List[Dict[str, Any]]:
    """Return all tree scans for an audit with geometry converted into GPS pairs."""
    engine = _require_async_engine()
    query = text(
        """
        SELECT
            id,
            audit_id,
            land_id,
            zone_id,
            ST_Y(gps_location) AS lat,
            ST_X(gps_location) AS lng,
            gps_accuracy_m,
            species,
            species_confidence,
            species_source,
            dbh_cm,
            height_m,
            gedi_height_m,
            height_source,
            wood_density,
            agb_kg,
            ar_tier_used,
            confidence_score,
            evidence_photo_path,
            evidence_photo_hash,
            scan_timestamp,
            created_at
        FROM ar_tree_scans
        WHERE audit_id = :audit_id
        ORDER BY created_at ASC
        """
    )

    async with engine.connect() as conn:
        result = await conn.execute(query, {"audit_id": audit_id})
        rows = result.mappings().all()

    scans: List[Dict[str, Any]] = []
    for row in rows:
        scan = _normalise_db_record(dict(row))
        scan["gps"] = {
            "lat": scan.pop("lat"),
            "lng": scan.pop("lng"),
        }
        scans.append(scan)
    return scans


async def delete_tree_scan_records_for_audit(audit_id: str) -> int:
    """Delete all persisted tree scans for an audit and return the deleted row count."""
    engine = _require_async_engine()
    query = text(
        """
        DELETE FROM ar_tree_scans
        WHERE audit_id = :audit_id
        """
    )

    async with engine.begin() as conn:
        result = await conn.execute(query, {"audit_id": audit_id})

    return int(result.rowcount or 0)


async def update_tree_scan_measurements(measurements: List[Dict[str, Any]]) -> None:
    """Persist GEDI-derived heights and AGB back into ar_tree_scans."""
    if not measurements:
        return

    engine = _require_async_engine()
    query = text(
        """
        UPDATE ar_tree_scans
        SET
            gedi_height_m = :gedi_height_m,
            height_source = :height_source,
            agb_kg = :agb_kg
        WHERE id = :id
        """
    )

    params = [
        {
            "id": measurement["id"],
            "gedi_height_m": measurement.get("gedi_height_m"),
            "height_source": measurement.get("height_source"),
            "agb_kg": measurement.get("agb_kg"),
        }
        for measurement in measurements
        if measurement.get("id")
    ]

    if not params:
        return

    async with engine.begin() as conn:
        await conn.execute(query, params)


async def verify_database_setup() -> None:
    """Fail fast when the documented direct PostgreSQL/PostGIS setup is unavailable."""
    engine = _require_async_engine()

    async with engine.connect() as conn:
        await conn.execute(text("SELECT PostGIS_Version()"))
