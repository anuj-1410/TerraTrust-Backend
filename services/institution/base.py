"""Shared content, evidence and public projection helpers."""
from uuid import uuid4

from app.institution_errors import fail
from models.institution.common import normalize
from models.institution.contracts import MODELS, inherited_fields
from repositories.institution.base import meta


def public(name, row, **overrides):
    values = {**meta(row), **normalize(row), **normalize(overrides)}
    # Only literal documented response keys leave the repository boundary.
    result = {}
    for key, field in MODELS[name].model_fields.items():
        if key in values:
            result[key] = values[key]
        elif not field.is_required():
            continue
        elif inherited_fields(name).get(key, {}).get("type", "").endswith("[]"):
            result[key] = []
        else:
            result[key] = None
    if name == "TreeResource" and result.get("data_origin") == "SYNTHETIC":
        # The first installed sample used an internal marker in typed metadata.
        # Project that marker without mutating persisted trees or capture hashes.
        metadata = dict(result.get("metadata") or {})
        if metadata.pop("sample", False):
            metadata.setdefault("notes", "Synthetic demonstration sample.")
        result["metadata"] = metadata
    return result


def draft(row, key="status"):
    if row[key] != "DRAFT":
        fail(409, "RESOURCE_FROZEN", "Create a correction revision before editing this record.")


async def evidence(repo, ids, parent_type, parent_id, iid, actor=None, *, required=False):
    if required and not ids:
        fail(422, "EVIDENCE_REQUIRED", "READY supporting evidence is required.")
    if len(set(map(str, ids))) != len(ids):
        fail(422, "DUPLICATE_EVIDENCE", "Evidence references must be unique.")
    result = []
    for eid in ids:
        item = await repo.get("institution_audit_evidence", eid)
        direct = item["parent_type"] == parent_type and str(item["parent_id"]) == str(parent_id)
        links = await repo.find("institution_evidence_links", {"evidence_id": eid, "parent_type": parent_type, "parent_id": parent_id}) if not direct else []
        linked_scope = any(str(link["institution_id"]) == str(iid) for link in links)
        if (not direct and not linked_scope) or (str(item.get("institution_id")) != str(iid) and not linked_scope) or (parent_type == "REGISTRATION_DRAFT" and str(item["owner_user_id"]) != str(actor["id"])):
            fail(404, "RESOURCE_NOT_FOUND", "Evidence is unavailable for this parent.")
        if item["state"] != "READY" or item.get("bytes_deleted_at") or not item.get("byte_hash"):
            fail(422, "EVIDENCE_NOT_READY", "Every selected evidence file must be verified and available.")
        result.append({"id": str(item["id"]), "sha256": item["byte_hash"], "size_bytes": item["actual_size_bytes"]})
    return result


async def link_evidence(repo, ids, parent_type, parent_id, iid, actor):
    for eid in ids:
        rows = await repo.find("institution_evidence_links", {"evidence_id": eid, "parent_type": parent_type, "parent_id": parent_id})
        if not rows:
            await repo.insert("institution_evidence_links", {"institution_id": iid, "evidence_id": eid, "parent_type": parent_type, "parent_id": parent_id, "linked_by": actor["id"]})


async def notification(repo, recipient, row, kind, resource_type, summary):
    await repo.insert("institution_notifications", {"institution_id": row.get("institution_id"), "recipient_user_id": recipient, "event_id": str(uuid4()), "type": kind, "resource_type": resource_type, "resource_id": row["id"], "revision": row.get("revision"), "safe_summary": summary})


async def case_for(repo, actor, table, row, resource_type, snapshot=None):
    iid = row["institution_id"]
    members = await repo.find("institution_users", {"institution_id": iid})
    events = await repo.rows("SELECT actor_id,details FROM audit_events WHERE institution_id=:iid AND (entity_id=:rid OR entity_type='institution_users')", {"iid": iid, "rid": row["id"]})
    contributors = {str(actor["id"]), str(row.get("created_by") or actor["id"])}
    contributors.update(str(e["actor_id"]) for e in events if e["actor_id"])
    evidence_ids = set(map(str, row.get("evidence_ids", row.get("control_evidence_ids", []))))
    def lineage(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"author_id", "created_by", "submitted_by"} and item:
                    contributors.add(str(item))
                elif key == "evidence_ids" and isinstance(item, list):
                    evidence_ids.update(map(str, item))
                elif (key in {"evidence_id", "source_evidence_id", "validation_artifact_id"} or key.endswith("_evidence_id")) and item:
                    evidence_ids.add(str(item))
                else:
                    lineage(item)
        elif isinstance(value, list):
            for item in value:
                lineage(item)
    lineage(snapshot or row)
    historical = {str(m["user_id"]) for m in members}
    historical.update(str(e["details"]["target_user_id"]) for e in events if e["details"].get("target_user_id"))
    old_cases = await repo.find("review_cases", {"institution_id": iid, "resource_type": resource_type, "resource_id": row["id"]}, lock=True)
    for old in old_cases:
        if old["status"] != "CLOSED":
            await repo.update("review_cases", old["id"], {"status": "CLOSED", "reason": "INPUT_SUPERSEDED"}, bump=True)
    case = await repo.insert("review_cases", {"institution_id": iid, "resource_type": resource_type, "resource_id": row["id"], "revision": row.get("revision", row["input_revision"]), "resource_input_revision": row["input_revision"], "input_hash": row.get("frozen_input_hash") or row.get("input_hash") or row["control_input_hash"], "result_hash": row.get("result_hash"), "resource_snapshot": {"resource": snapshot or normalize(row), "contributor_user_ids": sorted(contributors), "historical_member_user_ids": sorted(historical)}, "status": "OPEN", "field_issues": [], "evidence_ids": sorted(evidence_ids)})
    await repo.event(actor, "REVIEW_REQUESTED", table, row, details={"review_case_id": str(case["id"])})
    return case
