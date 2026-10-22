"""Read-only approved catalogs and independently scoped global source authoring."""
from app.institution_context import endpoint
from app.institution_access import operator_access, REGISTRY_CAPABILITIES
from app.institution_errors import fail
from services.institution import registry_service as service
from services.institution.factor_service import REGISTRIES
from services.institution.job_service import enqueue, accepted
from models.institution.common import digest, normalize

PATHS = {"FACTOR": "emission-factors", "METHOD": "carbon-methodologies", "MODEL": "models", "KNOWLEDGE": "knowledge-documents"}


def register(kind, path):
    table = REGISTRIES[kind]
    async def authorized(ctx):
        await operator_access(ctx.repo, ctx.actor, REGISTRY_CAPABILITIES[kind])
        rid = ctx.request.path_params.get("registry_id")
        if rid:
            ctx.registry = await ctx.repo.get(table, rid, lock=getattr(ctx, "lock_mode", False))

    @endpoint("GET", "/admin/" + path, prepare=authorized, query=("status",))
    async def list_registry(ctx, _):
        async def serialize(row):
            return service.resource(kind, row)
        filters = {}
        if ctx.request.query_params.get("status"):
            filters["status"] = ctx.request.query_params["status"]
        return await ctx.page(table, filters, serialize)

    @endpoint("POST", "/admin/" + path, model="RegistryDraft", status=201, prepare=authorized)
    async def create_registry(ctx, body):
        return await service.create(ctx, body, kind)

    @endpoint("GET", "/admin/" + path + "/{registry_id}", prepare=authorized)
    async def get_registry(ctx, _):
        return service.resource(kind, ctx.registry)

    @endpoint("PATCH", "/admin/" + path + "/{registry_id}", prepare=authorized)
    async def patch_registry(ctx, body):
        return await service.patch(ctx, body, kind)

    action = "activate" if kind in {"MODEL", "KNOWLEDGE"} else "publish"
    @endpoint("POST", "/admin/" + path + "/{registry_id}/" + action, model="RegistryAction", prepare=authorized)
    async def publish_registry(ctx, body):
        return await service.publish(ctx, body, kind)

    if kind == "KNOWLEDGE":
        @endpoint("POST", "/admin/" + path + "/{registry_id}/reindex", model="ReindexRequest", status=202, prepare=authorized)
        async def reindex(ctx, body):
            row = ctx.registry
            ctx.match(row)
            if row["artifact_hash"] != body["expected_artifact_hash"] or row["status"] != "DRAFT":
                fail(409, "ARTIFACT_HASH_CHANGED", "Ingest an unchanged unpublished source version.")
            if str(row["author_id"]) != str(ctx.actor["id"]):
                fail(403,"PERMISSION_DENIED","Only the source author may prepare this unpublished corpus.")
            if body.get("ingestion_config_version") not in {None, row["config"].get("ingestion_config_version")}:
                fail(409,"INGESTION_CONFIG_CHANGED","Refresh the draft to use its application-owned ingestion version.")
            if not row["source_evidence_id"] and row["config"].get("source_type") != "URL":
                fail(422, "SOURCE_BYTES_REQUIRED", "Upload the actual licensed source bytes before ingestion.")
            from services.advisory.ingestion_rules import configuration
            rules = await configuration(ctx.repo)
            frozen = {"input_hash": digest({"document_hash": row["artifact_hash"], "ingestion_rules_hash": rules["artifact_hash"]}), "document": normalize(row), "request": body, "ingestion_rules": rules}
            job = await enqueue(ctx, "knowledge_ingestion", row, frozen, asset_type="KNOWLEDGE")
            return accepted(job)


for kind, path in PATHS.items():
    register(kind, path)


async def catalog_access(ctx):
    rows = await ctx.repo.rows("SELECT iu.id FROM institution_users iu JOIN institutions i ON i.id=iu.institution_id WHERE iu.user_id=:uid AND iu.status='ACTIVE' AND iu.permission IN ('OWNER','MANAGER') AND i.verification_status='APPROVED' LIMIT 1", {"uid": ctx.actor["id"]})
    if not rows:
        grants = await ctx.repo.rows("SELECT id FROM operator_grants WHERE user_id=:uid AND capability IN ('FACTOR_REVIEW','METHOD_REVIEW','MODEL_REVIEW') AND institution_id IS NULL AND revoked_at IS NULL AND valid_from<=now() AND valid_until>now()", {"uid": ctx.actor["id"]})
        if not grants:
            fail(403, "PERMISSION_DENIED", "Approved catalog access requires operational membership or a registry grant.")


@endpoint("GET", "/emission-factors", prepare=catalog_access, query=("category", "unit", "geography", "activity_date", "method_version"))
async def public_factors(ctx, _):
    from services.institution.runtime_configuration import current_method
    await current_method(ctx.repo, ctx.actor, required=False)
    rows = await ctx.repo.find("emission_factors", {"status": "APPROVED", **({"data_origin": "LIVE"} if not ctx.generation else {})})
    params = ctx.request.query_params
    method = await ctx.repo.get("carbon_methodologies", params["method_version"]) if params.get("method_version") else None
    allowed = {str(fid) for mapping in method["config"]["factor_mappings"] for fid in mapping["allowed_factor_ids"]} if method else None
    rows = [r for r in rows if (not params.get("category") or r["config"]["category"] == params["category"]) and (not params.get("unit") or r["config"]["denominator_unit"] == params["unit"]) and (not params.get("geography") or params["geography"] in r["geography_codes"]) and (allowed is None or str(r["id"]) in allowed) and (not params.get("activity_date") or (not r["valid_from"] or str(r["valid_from"]) <= params["activity_date"]) and (not r["valid_to"] or str(r["valid_to"]) >= params["activity_date"]))]
    return await ctx.page_rows(rows, lambda row: service.resource("FACTOR", row))


@endpoint("GET", "/carbon-methodologies", prepare=catalog_access, query=("category", "domain"))
async def public_methods(ctx, _):
    from services.institution.runtime_configuration import current_method
    current = await current_method(ctx.repo, ctx.actor)
    params = ctx.request.query_params
    if params.get("domain") and params["domain"] != "INSTITUTION":
        return await ctx.page_rows([])
    async def serialize(row):
        output = service.resource("METHOD", row)
        # Exact child IDs are part of the approved immutable config/dictionary.
        policies = await ctx.repo.find("method_quality_policies", {"methodology_id": row["id"]})
        output["quality_policy_versions"] = [{"id": str(p["id"]), "artifact_hash": p["artifact_hash"]} for p in policies]
        return output
    rows = await ctx.repo.find("carbon_methodologies", {"status": "APPROVED", **({"data_origin": "LIVE"} if not ctx.generation else {})})
    rows.sort(key=lambda row: str(row["id"]) != str(current["id"]))
    if params.get("category"):
        rows = [row for row in rows if params["category"] in row["config"]["coverage_categories"]]
    return await ctx.page_rows(rows, serialize)


@endpoint("GET", "/carbon-methodologies/{method_id}", prepare=catalog_access)
async def exact_method(ctx, _):
    from services.institution.factor_service import approved
    row = await approved(ctx.repo, "carbon_methodologies", ctx.request.path_params["method_id"])
    output = service.resource("METHOD", row)
    output["quality_policy_versions"] = [{"id": str(p["id"]), "artifact_hash": p["artifact_hash"]} for p in await ctx.repo.find("method_quality_policies", {"methodology_id": row["id"]})]
    return output
