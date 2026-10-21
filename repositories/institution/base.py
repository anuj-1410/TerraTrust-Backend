"""Parameterized SQL over a fixed, reviewed physical table dictionary."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

from app.institution_errors import fail
from models.institution.common import normalize, digest

TABLES = json.loads(Path(__file__).with_name("table_definitions.json").read_text(encoding="utf-8"))
for field in ("stock_tC", "signed_change_tCO2", "stock_gain_tCO2", "stock_loss_tCO2", "removal_tCO2", "release_tCO2"):
    TABLES["campus_tree_audits"][field] = {"type": "NUMERIC(24,12)", "nullable": True, "rule": "BSDD production contract addendum; nullable sourced stock/change"}
TABLES["campus_tree_measurements"]["last_survey_input_revision"] = {"type":"INTEGER","nullable":False,"rule":"Server-assigned parent counter for synchronization deltas; DEFAULT 1"}
PHYSICAL_NAMES = {key.lower(): key for fields in TABLES.values() for key in fields if key.lower() != key}
GLOBAL_TABLES = set("emission_factors carbon_methodologies model_registry method_parameters method_quality_policies knowledge_documents knowledge_chunks knowledge_corpus_versions knowledge_embeddings institution_deployment_policies operator_grants processing_jobs job_outbox audit_events idempotency_records institution_audit_evidence institution_evidence_links institution_notifications institution_deletion_requests institution_registration_authorizations".split())
GLOBAL_TABLES.update({"institution_calculation_settings", "institution_formula_values", "institution_knowledge_sources"})


def columns(table):
    if table not in TABLES or table == "users":
        raise ValueError("Unapproved institution table.")
    return {"id", "created_at", "updated_at", "input_revision", "data_origin", *TABLES[table],
            *({"institution_id"} if table not in GLOBAL_TABLES and table != "institutions" else set())}


def clean(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


class Repository:
    def __init__(self, conn):
        self.conn = conn

    async def rows(self, sql, params=None):
        result = await self.conn.execute(text(sql), params or {})
        return [{PHYSICAL_NAMES.get(key, key): value for key, value in row.items()} for row in result.mappings().all()]

    async def one(self, sql, params=None, *, required=True):
        rows = await self.rows(sql, params)
        if not rows:
            if required:
                fail(404, "RESOURCE_NOT_FOUND", "Resource is unavailable.")
            return None
        return rows[0]

    def selection(self, table):
        columns(table)
        extras = [f"ST_AsGeoJSON({name})::jsonb AS {name}" for name, spec in TABLES[table].items() if spec["type"].startswith("geometry(")]
        return "*" + (", " + ", ".join(extras) if extras else "")

    async def get(self, table, resource_id, *, lock=False, required=True):
        return await self.one(f"SELECT {self.selection(table)} FROM {table} WHERE id=:id" + (" FOR UPDATE" if lock else ""), {"id": resource_id}, required=required)

    async def find(self, table, filters, *, lock=False, limit=None):
        if not set(filters) <= columns(table):
            raise ValueError("Unknown filter.")
        predicates = [f"{key} IS NULL" if value is None else f"{key}=:{key}" for key, value in filters.items()]
        return await self.rows(f"SELECT {self.selection(table)} FROM {table} WHERE " + (" AND ".join(predicates) or "TRUE") + " ORDER BY created_at DESC,id DESC" + (" LIMIT :_limit" if limit is not None else "") + (" FOR UPDATE" if lock else ""), {**filters, **({"_limit": limit} if limit is not None else {})})

    def binds(self, table, values):
        if not set(values) <= columns(table):
            raise ValueError(f"Unknown columns for {table}: {set(values) - columns(table)}")
        expressions, params = {}, {}
        for key, value in values.items():
            spec = TABLES[table].get(key, {}).get("type", "TIMESTAMPTZ" if key in {"created_at", "updated_at"} else "")
            expr = ":" + key
            if spec == "JSONB":
                value = json.dumps(normalize(value), ensure_ascii=False) if value is not None else None
                expr = f"CAST(:{key} AS jsonb)"
            elif spec.startswith("geometry("):
                value = json.dumps(value) if value is not None else None
                expr = f"ST_SetSRID(ST_GeomFromGeoJSON(:{key}),4326)"
                if "MultiPolygon" in spec:
                    expr = f"ST_Multi({expr})"
            elif spec.startswith("NUMERIC") and value is not None:
                value = Decimal(str(value))
            elif spec == "DATE" and isinstance(value, str):
                from datetime import date
                value = date.fromisoformat(value)
            elif spec == "TIMESTAMPTZ" and isinstance(value, str):
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            elif spec in {"UUID[]", "TEXT[]"}:
                expr = f"CAST(:{key} AS {spec})"
            expressions[key], params[key] = expr, value
        return expressions, params

    async def insert(self, table, values):
        values = {"id": str(uuid4()), **values}
        expressions, params = self.binds(table, values)
        await self.conn.execute(text(f"INSERT INTO {table} ({','.join(expressions)}) VALUES ({','.join(expressions.values())})"), params)
        return await self.get(table, values["id"])

    async def update(self, table, resource_id, values, *, bump=False):
        expressions, params = self.binds(table, values)
        assignments = [f"{key}={value}" for key, value in expressions.items()]
        if bump:
            assignments.append("input_revision=input_revision+1")
        assignments.append("updated_at=now()")
        await self.conn.execute(text(f"UPDATE {table} SET {','.join(assignments)} WHERE id=:_id"), {**params, "_id": resource_id})
        return await self.get(table, resource_id)

    async def event(self, actor, action, table, row, *, old_hash=None, reason=None, details=None):
        return await self.insert("audit_events", {"institution_id": row.get("institution_id") or (row["id"] if table == "institutions" else None), "domain": "INSTITUTION", "actor_id": actor["id"] if actor else None, "action": action, "entity_type": table, "entity_id": row["id"], "old_hash": old_hash, "new_hash": row.get("input_hash") or row.get("artifact_hash"), "reason": reason, "details": details or {}, "event_at": datetime.now(timezone.utc)})


def meta(row):
    return {key: normalize(row.get(key, default)) for key, default in {"id": None, "institution_id": None, "input_revision": 1, "created_at": None, "updated_at": None, "data_origin": "LIVE"}.items()} | {"schema_version": 1}
