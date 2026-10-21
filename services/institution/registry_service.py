"""Independent publication of immutable factors, methods, models and sources."""
from datetime import datetime, timezone
from uuid import uuid4

from app.institution_access import REGISTRY_CAPABILITIES, operator_access
from app.institution_errors import fail
from models.institution.common import digest, normalize
from models.institution.contracts import parse
from services.institution.base import public, evidence
from services.institution.factor_service import REGISTRIES, CONFIGS, validate_config, compile_schema


def resource(kind, row):
    return public("RegistryResource", row, kind=kind)


def registry_hash(row, source_hash=None):
    fields = ("logical_key", "version", "schema_version", "title", "publisher", "source_url", "source_evidence_id", "source_version", "published_date", "license", "source_page", "source_section", "geography_codes", "valid_from", "valid_to", "applicability_notes", "config")
    return digest({**{key: normalize(row.get(key)) for key in fields}, "source_byte_hash": source_hash})


async def create(ctx, body, kind):
    if body["kind"] != kind:
        fail(422, "REGISTRY_KIND_MISMATCH", "Registry kind does not match this endpoint.")
    from services.institution.farmer_biomass import ENGINE
    if body["logical_key"].startswith("DB:") or (kind == "MODEL" and body["logical_key"] == ENGINE):
        fail(422, "SERVER_OWNED_REGISTRY_KEY", "This registry family is assigned automatically from application/database configuration.")
    if kind == "KNOWLEDGE":
        from app.institution_rules import RETRIEVAL_RULES_ID
        body = {**body, "config": {**body["config"], "ingestion_config_version": RETRIEVAL_RULES_ID}}
    validate_config(kind, body["config"], draft=True)
    if body["source_evidence_id"] is not None:
        fail(422, "SOURCE_PARENT_REQUIRED", "Create the draft source parent before uploading its bytes.")
    values = {key: value for key, value in body.items() if key != "kind"}
    row = await ctx.repo.insert(REGISTRIES[kind], {**values, "schema_version": 1, "status": "DRAFT", "artifact_hash": registry_hash(values), "author_id": ctx.actor["id"]})
    await ctx.repo.event(ctx.actor, "REGISTRY_DRAFT_CREATED", REGISTRIES[kind], row)
    ctx.revision = 1
    return resource(kind, row)


async def patch(ctx, body, kind):
    row = ctx.registry
    if kind == "KNOWLEDGE" and "config" in body:
        from app.institution_rules import RETRIEVAL_RULES_ID
        body = {**body, "config": {**body["config"], "ingestion_config_version": RETRIEVAL_RULES_ID}}
    ctx.match(row)
    if row["status"] != "DRAFT" or str(row["author_id"]) != str(ctx.actor["id"]):
        fail(403, "PERMISSION_DENIED", "Only the original author may edit an unpublished draft.")
    if set(body) & {"kind", "logical_key", "version", "author_id", "status"}:
        fail(422, "IMMUTABLE_REGISTRY_IDENTITY", "Create a new registry version to change its identity.")
    allowed = set(parse.__globals__["MODELS"]["RegistryDraft"].model_fields) - {"kind", "logical_key", "version"}
    if not body or set(body) - allowed:
        fail(422, "INVALID_PAYLOAD", "Unknown registry draft fields.")
    source = {**normalize(row), **body}
    parse("RegistryDraft", {"kind": kind, **{key: source.get(key) for key in allowed | {"logical_key", "version"}}})
    validate_config(kind, source["config"], draft=True)
    byte_hash = None
    if source["source_evidence_id"]:
        parent = {"FACTOR": "FACTOR_SOURCE", "METHOD": "METHOD_SOURCE", "MODEL": "MODEL", "KNOWLEDGE": "KNOWLEDGE"}[kind]
        manifest = await evidence(ctx.repo, [source["source_evidence_id"]], parent, row["id"], None)
        byte_hash = manifest[0]["sha256"]
    saved = await ctx.repo.update(REGISTRIES[kind], row["id"], {**body, "artifact_hash": registry_hash(source, byte_hash)}, bump=True)
    await ctx.repo.event(ctx.actor, "REGISTRY_DRAFT_SAVED", REGISTRIES[kind], saved)
    ctx.revision = saved["input_revision"]
    return resource(kind, saved)


async def publish(ctx, body, kind):
    row = ctx.registry
    ctx.match(row)
    if str(row["author_id"]) == str(ctx.actor["id"]):
        fail(403, "REVIEWER_CONFLICT", "A source author cannot publish their own version.")
    if body["expected_artifact_hash"] != row["artifact_hash"]:
        fail(409, "ARTIFACT_HASH_CHANGED", "Review the current source/configuration before publication.")
    action = body["action"]
    if action == "DEACTIVATE":
        if row["status"] not in {"ACTIVE", "APPROVED"}:
            fail(409, "INVALID_REGISTRY_STATE", "Only a published source may be deactivated.")
        saved = await ctx.repo.update(REGISTRIES[kind], row["id"], {"status": "DEACTIVATED"})
        await ctx.repo.event(ctx.actor, "REGISTRY_DEACTIVATED", REGISTRIES[kind], saved, reason=body["reason"])
        return resource(kind, saved)
    if row["status"] != "DRAFT" or action not in {"PUBLISH", "ACTIVATE", "SUPERSEDE"}:
        fail(409, "INVALID_REGISTRY_STATE", "Publish a new independently reviewed draft version.")
    config = validate_config(kind, row["config"])
    parent = {"FACTOR": "FACTOR_SOURCE", "METHOD": "METHOD_SOURCE", "MODEL": "MODEL", "KNOWLEDGE": "KNOWLEDGE"}[kind]
    if not row["source_evidence_id"]:
        fail(422, "SOURCE_BYTES_REQUIRED", "Actual reviewed source bytes are required.")
    source_manifest = await evidence(ctx.repo, [str(row["source_evidence_id"])], parent, row["id"], None, required=True)
    if registry_hash(row, source_manifest[0]["sha256"]) != row["artifact_hash"]:
        fail(409, "ARTIFACT_HASH_CHANGED", "Save the actual source checksum into the draft before publication.")
    if kind == "METHOD":
        for policy in (config["ranking_config"], config["uncertainty_policy"], config["attribution_policy"]):
            metadata = policy["source_metadata"]
            actual = await evidence(ctx.repo, [metadata["evidence_id"]], "METHOD_SOURCE", row["id"], None, required=True)
            if metadata["checksum"] != actual[0]["sha256"]:
                fail(422, "INVALID_POLICY_SOURCE", "Policy rationale must match its actual reviewed source bytes.")
            if policy.get("validation_artifact_id"):
                await evidence(ctx.repo, [policy["validation_artifact_id"]], "METHOD_SOURCE", row["id"], None, required=True)
        processing = config.get("campus_processing")
        if processing:
            source_ids = [processing["mask_validation_evidence_id"], *[stratum["evidence_id"] for stratum in processing["strata"]]]
            await evidence(ctx.repo, list(dict.fromkeys(source_ids)), "METHOD_SOURCE", row["id"], None, required=True)
        for param in config["parameters"]:
            existing = await ctx.repo.get("method_parameters", param["id"], required=False)
            if existing:
                fail(409, "PARAMETER_ID_IN_USE", "New versions require new method parameter identities.")
            metadata = param["source_metadata"]
            source = await evidence(ctx.repo, [metadata["evidence_id"]], "METHOD_SOURCE", row["id"], None, required=True)
            if metadata["checksum"] != source[0]["sha256"] or (param["kind"] != "ALLOMETRY" and param["value"] is None):
                fail(422, "INVALID_PARAMETER_SOURCE", "Every scalar parameter requires actual reviewed source bytes and a value.")
            compile_schema(param["required_inputs"])
            compile_schema(param["applicability"])
            await ctx.repo.insert("method_parameters", {"methodology_id": row["id"], **param})
        await ctx.repo.insert("method_quality_policies", {"methodology_id": row["id"], "version": row["version"], "config": config["quality_policy"], "artifact_hash": digest(config["quality_policy"])})
    elif kind == "MODEL":
        model_source = await evidence(ctx.repo, [config["artifact_id"]], "MODEL", row["id"], None, required=True)
        if model_source[0]["sha256"] != config["artifact_sha256"]:
            fail(422, "MODEL_HASH_MISMATCH", "Model artifact hash differs from the reviewed bytes.")
        await evidence(ctx.repo, [config["validation_artifact_id"]], "MODEL", row["id"], None, required=True)
    elif kind == "KNOWLEDGE":
        candidates = await ctx.repo.rows("SELECT * FROM knowledge_corpus_versions WHERE author_id=:author AND status='DRAFT' AND manifest @> CAST(:match AS jsonb) ORDER BY created_at DESC LIMIT 1", {"author": row["author_id"], "match": __import__("json").dumps({"document_id": str(row["id"])})})
        if not candidates:
            fail(422, "CORPUS_NOT_READY", "Ingest and validate a candidate corpus before activation.")
        candidate = candidates[0]
        if candidate["manifest"].get("processing_status") != "SUCCEEDED":
            fail(422, "CORPUS_NOT_READY", "Candidate ingestion is not successful.")
        processing_job = await ctx.repo.get("processing_jobs", candidate["manifest"]["processing_job_id"])
        if processing_job["status"] != "SUCCEEDED" or candidate["manifest"].get("source_artifact_hash") != row["artifact_hash"] or candidate["manifest"].get("source_checksum") != source_manifest[0]["sha256"]:
            fail(409,"CORPUS_SOURCE_CHANGED","Activate only the exact successfully ingested source version.")
        await ctx.repo.update("knowledge_corpus_versions", candidate["id"], {"status": "ACTIVE", "published_at": datetime.now(timezone.utc), "reviewer_id": ctx.actor["id"]})
    if body["supersedes_id"]:
        prior = await ctx.repo.get(REGISTRIES[kind], body["supersedes_id"], lock=True)
        if prior["logical_key"] != row["logical_key"] or prior["version"] >= row["version"]:
            fail(422, "INVALID_SUPERSESSION", "Superseded source must be an older version in this registry family.")
        await ctx.repo.update(REGISTRIES[kind], prior["id"], {"status": "SUPERSEDED"})
    saved = await ctx.repo.update(REGISTRIES[kind], row["id"], {"config": config, "status": "ACTIVE" if kind in {"MODEL", "KNOWLEDGE"} else "APPROVED", "reviewer_id": ctx.actor["id"], "validated_at": datetime.now(timezone.utc), "supersedes_id": body["supersedes_id"]})
    await ctx.repo.event(ctx.actor, "REGISTRY_PUBLISHED", REGISTRIES[kind], saved, reason=body["reason"])
    ctx.revision = saved["input_revision"]
    return resource(kind, saved)
