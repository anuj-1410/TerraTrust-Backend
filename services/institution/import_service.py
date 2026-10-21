"""Immutable RFC4180 CSV previews with all-or-nothing draft commits."""
import csv
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import hashlib
import io
import json
from uuid import uuid4

from app.institution_access import require_flow
from app.institution_errors import fail, InstitutionError
from models.institution.common import digest, normalize
from models.institution.contracts import parse
from services.institution.base import draft, evidence
from services.institution.factor_service import approved, resolve_factor, compile_schema
from services.institution.accounting_service import validate_lines
from services.institution.annual_service import line_public, refresh_hash
from services.shared.evidence_service import read_bytes

COLUMNS = ["line_id", "category", "subcategory", "campus_id", "source_id", "activity_start", "activity_end", "quantity", "unit", "source_type", "estimate_notes", "evidence_ids", "included_in_line_id", "allocation_ratio", "details_json"]


async def preview(ctx, body):
    require_flow()
    audit = ctx.parent
    ctx.match(audit)
    draft(audit, "business_status")
    required = {"evidence_id", "category", "column_mapping", "expected_input_revision", "operation"}
    if not body or set(body) != required or body["operation"] != "APPEND_NEW" or body["expected_input_revision"] != audit["input_revision"]:
        fail(422, "INVALID_IMPORT", "Supply the exact append-new CSV preview fields and current revision.")
    await evidence(ctx.repo, [body["evidence_id"]], "AUDIT", audit["id"], ctx.iid, required=True)
    file = await ctx.repo.get("institution_audit_evidence", body["evidence_id"])
    if file["mime_type"] != "text/csv":
        fail(422, "CSV_REQUIRED", "Select a verified UTF-8 CSV under this audit.")
    data = await read_bytes(file["private_bucket"], file["object_key"])
    if hashlib.sha256(data).hexdigest() != file["byte_hash"]:
        fail(422, "SOURCE_BYTES_CHANGED", "CSV bytes differ from the verified evidence.")
    method = await approved(ctx.repo, "carbon_methodologies", audit["method_version"])
    existing = await ctx.repo.find("institution_emission_inputs", {"audit_id": audit["id"]})
    existing_ids = {str(row["id"]) for row in existing}
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""), strict=True)
    mapping = body["column_mapping"]
    if not isinstance(mapping, dict) or set(mapping) - set(COLUMNS) or any(not isinstance(v, str) for v in mapping.values()):
        fail(422, "INVALID_COLUMN_MAPPING", "Map only the declared CSV columns.")
    names = [mapping.get(column, column) for column in COLUMNS]
    if reader.fieldnames is None or len(set(names)) != len(COLUMNS) or len(set(reader.fieldnames)) != len(reader.fieldnames) or set(reader.fieldnames) != set(names):
        fail(422, "CSV_COLUMNS_INVALID", "CSV headers must match the complete selected template/mapping.")
    rows, accepted_lines, duplicate_count = [], [], 0
    for row_index, source in enumerate(reader, start=2):
        if row_index > 10001:
            fail(413, "CSV_TOO_MANY_ROWS", "CSV exceeds 10000 rows.")
        errors, warnings, item = [], [], None
        rid = str(uuid4())
        try:
            values = {column: source[mapping.get(column, column)] for column in COLUMNS}
            if None in source or any(value is None for value in values.values()):
                raise ValueError("CSV field count differs from its header.")
            rid = values["line_id"] or rid
            item = {"id": rid, "category": values["category"], "subcategory": values["subcategory"], "campus_id": values["campus_id"] or None, "source_id": values["source_id"], "quantity": values["quantity"] or None, "unit": values["unit"], "activity_start": values["activity_start"], "activity_end": values["activity_end"], "source_type": values["source_type"], "estimate_notes": values["estimate_notes"] or None, "evidence_ids": json.loads(values["evidence_ids"] or "[]"), "included_in_input_id": values["included_in_line_id"] or None, "allocation_ratio": values["allocation_ratio"] or None, "details": json.loads(values["details_json"])}
            parse("EmissionLine", item)
            if date.fromisoformat(item["activity_end"]) > datetime.now(ZoneInfo(audit["timezone"])).date():
                raise ValueError("Future activity is not an actual imported source.")
            if item["category"] != body["category"]:
                raise ValueError("CSV row category differs from the selected template.")
            if rid in existing_ids or any(r["id"] == rid for r in accepted_lines):
                duplicate_count += 1
                raise ValueError("Input identity already exists; import only new sources.")
            await evidence(ctx.repo, item["evidence_ids"], "AUDIT", audit["id"], ctx.iid)
            item["factor_id"] = await resolve_factor(ctx.repo, method, item)
            schema = method["config"]["activity_schemas"].get(item["category"])
            if not schema or list(compile_schema(schema).iter_errors(item["details"])):
                raise ValueError("Row does not match the approved activity schema.")
            overlap = validate_lines(audit, existing + accepted_lines + [item])
            if overlap:
                duplicate_count += int(any(e["code"] == "OVERLAPPING_ACTIVITY" for e in overlap))
                errors.extend(overlap)
            if not errors:
                accepted_lines.append(item)
        except (ValueError, KeyError, TypeError, InstitutionError) as exc:
            errors.append({"path": f"/rows/{row_index}", "code": "INVALID_CSV_ROW", "message": "Correct this row's types, units, source dates and references."})
        from models.institution.common import uuid_text
        try:
            uuid_text(rid)
        except (ValueError, TypeError):
            rid, item = str(uuid4()), None
        rows.append({"row_number": row_index, "line_id": rid, "normalized_line": item, "errors": errors, "warnings": warnings})
    if not rows:
        fail(422, "CSV_EMPTY", "Import at least one new activity row.")
    normalized_hash = digest(rows)
    batch = await ctx.repo.insert("institution_import_batches", {"institution_id": ctx.iid, "audit_id": audit["id"], "evidence_id": file["id"], "source_input_revision": audit["input_revision"], "file_hash": file["byte_hash"], "normalized_hash": normalized_hash, "column_mapping": mapping, "schema_version": 1, "operation": "APPEND_NEW", "status": "PREVIEWED", "expires_at": datetime.now(timezone.utc) + timedelta(minutes=30), "created_by": ctx.actor["id"], "valid_count": sum(not r["errors"] for r in rows), "error_count": sum(bool(r["errors"]) for r in rows), "duplicate_count": duplicate_count})
    for row in rows:
        await ctx.repo.insert("institution_import_rows", {"institution_id": ctx.iid, "audit_id": audit["id"], "batch_id": batch["id"], **{**row, "normalized_line": row["normalized_line"] or {}}})
    page = await import_resource(ctx, audit, batch)
    return {**page, "preview_id": str(batch["id"]), "valid_count": batch["valid_count"], "error_count": batch["error_count"], "duplicate_count": batch["duplicate_count"]}


async def import_resource(ctx, audit, batch):
    if str(batch["audit_id"]) != str(audit["id"]):
        fail(404, "RESOURCE_NOT_FOUND", "Import is unavailable.")
    async def serialize(row):
        return {"row_number": row["row_number"], "line_id": str(row["line_id"]) if row["normalized_line"] else None, "normalized_line": row["normalized_line"] or None, "errors": row["errors"], "warnings": row["warnings"]}
    rows = await ctx.repo.find("institution_import_rows", {"batch_id": batch["id"]})
    selected = ctx.request.query_params.get("errors_only")
    if selected is not None:
        if selected not in {"true", "false"}:
            fail(422, "INVALID_FILTER", "errors_only must be true or false.")
        if selected == "true":
            rows = [row for row in rows if row["errors"]]
    rows.sort(key=lambda row: row["row_number"])
    page = await ctx.page_rows(rows, serialize)
    total = batch["valid_count"] + batch["error_count"]
    return {"batch_id": str(batch["id"]), "status": batch["status"], "expires_at": normalize(batch["expires_at"]), "input_revision": batch["source_input_revision"], "file_hash": batch["file_hash"], "normalized_hash": batch["normalized_hash"], "counts": {"total_rows": total, "valid_rows": batch["valid_count"], "error_rows": batch["error_count"], "warning_rows": 0}, "can_commit": batch["status"] == "PREVIEWED" and batch["error_count"] == 0 and batch["duplicate_count"] == 0 and batch["expires_at"] > datetime.now(timezone.utc) and batch["source_input_revision"] == audit["input_revision"] and audit["business_status"] == "DRAFT", "rows": page, "committed_input_revision": batch["committed_input_revision"]}


async def commit(ctx, body):
    require_flow()
    audit = ctx.parent
    ctx.match(audit)
    draft(audit, "business_status")
    if not body or set(body) != {"preview_id", "normalized_hash", "expected_input_revision"}:
        fail(422, "INVALID_IMPORT_COMMIT", "Commit the immutable preview/hash/revision without editable rows.")
    batch = await ctx.repo.get("institution_import_batches", body["preview_id"], lock=True)
    check = await import_resource(ctx, audit, batch)
    if not check["can_commit"] or batch["normalized_hash"] != body["normalized_hash"] or audit["input_revision"] != body["expected_input_revision"]:
        fail(409, "IMPORT_PREVIEW_STALE", "Preview expired or changed; request a new validated preview.")
    rows = await ctx.repo.find("institution_import_rows", {"batch_id": batch["id"]})
    existing = await ctx.repo.find("institution_emission_inputs", {"audit_id": audit["id"]})
    await evidence(ctx.repo, [str(batch["evidence_id"])], "AUDIT", audit["id"], ctx.iid, required=True)
    method = await approved(ctx.repo, "carbon_methodologies", audit["method_version"])
    items = [row["normalized_line"] for row in rows]
    errors = validate_lines(audit, existing + items)
    if errors:
        fail(409, "IMPORT_SOURCE_CONFLICT", "Current sources overlap this import.", fields=errors)
    for item in items:
        await evidence(ctx.repo, item["evidence_ids"], "AUDIT", audit["id"], ctx.iid)
        selected = await resolve_factor(ctx.repo, method, item)
        if selected != item.get("factor_id"):
            fail(409, "IMPORT_PREVIEW_STALE", "An approved factor changed; validate a fresh CSV preview.")
        schema = method["config"]["activity_schemas"].get(item["category"])
        if not schema or list(compile_schema(schema).iter_errors(item["details"])):
            fail(409, "IMPORT_PREVIEW_STALE", "The method no longer accepts these activity details.")
    for item in items:
        await ctx.repo.insert("institution_emission_inputs", {"institution_id": ctx.iid, "audit_id": audit["id"], **item, "created_by": ctx.actor["id"], "flags": []})
    audit = await refresh_hash(ctx, audit)
    await ctx.repo.update("institution_import_batches", batch["id"], {"status": "COMMITTED", "committed_input_revision": ctx.revision, "committed_by": ctx.actor["id"], "committed_at": datetime.now(timezone.utc)})
    return {"batch_id": str(batch["id"]), "status": "COMMITTED", "created_count": len(items), "input_revision": ctx.revision}
