"""Private advisor history and exact-final-context publication."""
from datetime import datetime, timezone

from app.institution_context import endpoint
from app.institution_access import require_flow
from app.institution_errors import fail
from services.advisory import advisory_service as service
from services.institution.base import public


async def ask_access(ctx):
    await service.resolve_context(ctx, ctx.body)


@endpoint("POST", "/advisory/ask", model="AskRequest", status=202, prepare=ask_access)
async def ask(ctx, body):
    return await service.ask(ctx, body)


async def query_access(ctx):
    row = await ctx.access("advisory_queries", ctx.request.path_params["qid"])
    owner = ctx.member["permission"] == "OWNER"
    if str(row["user_id"]) != str(ctx.actor["id"]) and not owner and row["publication_state"] != "PUBLISHED":
        fail(404, "RESOURCE_NOT_FOUND", "Advice is unavailable.")
    if ctx.member["permission"] == "VIEWER" and row["publication_state"] != "PUBLISHED":
        fail(404, "RESOURCE_NOT_FOUND", "Advice is unavailable.")
    if row["publication_state"] == "PUBLISHED":
        audit = await ctx.repo.get("institution_audits", row["audit_id"])
        if audit["business_status"] not in {"FINALIZED", "SUPERSEDED"} or str(audit["reviewed_snapshot_id"]) != str(row["snapshot_id"]):
            fail(404, "RESOURCE_NOT_FOUND", "Advice is unavailable.")
    ctx.query = row


@endpoint("GET", "/advisory/{qid}", prepare=query_access)
async def get_query(ctx, _):
    ctx.revision = ctx.query["input_revision"]
    return service.resource(ctx.query)


async def history_access(ctx):
    await ctx.access("institutions", ctx.request.path_params["iid"])


@endpoint("GET", "/institutions/{iid}/advisory-history", prepare=history_access, query=("audit_id", "publication_state"))
async def history(ctx, _):
    rows = await ctx.repo.find("advisory_queries", {"institution_id": ctx.iid})
    rows = [r for r in rows if (ctx.member["permission"] == "OWNER" or str(r["user_id"]) == str(ctx.actor["id"]) or r["publication_state"] == "PUBLISHED") and (ctx.member["permission"] != "VIEWER" or r["publication_state"] == "PUBLISHED")]
    authorized = []
    for row in rows:
        if row["publication_state"] == "PUBLISHED":
            audit = await ctx.repo.get("institution_audits", row["audit_id"])
            if audit["business_status"] not in {"FINALIZED", "SUPERSEDED"} or str(audit["reviewed_snapshot_id"]) != str(row["snapshot_id"]):
                continue
        authorized.append(row)
    rows = authorized
    for key in ("audit_id", "publication_state"):
        if ctx.request.query_params.get(key):
            rows = [r for r in rows if str(r[key]) == ctx.request.query_params[key]]
    return await ctx.page_rows(rows, lambda row: {"query_id": str(row["id"]), "audit_id": str(row["audit_id"]), "revision": row["context_snapshot"]["revision"], "snapshot_id": str(row["snapshot_id"]), "status": row["status"], "publication_state": row["publication_state"], "question_preview": service.redact(row["question"])[:160], "created_at": row["created_at"], "warnings": row["warnings"]})


@endpoint("POST", "/advisory/{qid}/feedback", model="FeedbackRequest", prepare=query_access)
async def feedback(ctx, body):
    rows = await ctx.repo.find("advisory_feedback", {"query_id": ctx.query["id"], "user_id": ctx.actor["id"]})
    if rows:
        await ctx.repo.update("advisory_feedback", rows[0]["id"], body)
    else:
        await ctx.repo.insert("advisory_feedback", {"institution_id": ctx.iid, "query_id": ctx.query["id"], "user_id": ctx.actor["id"], **body})
    return {"saved": True}


async def publisher(ctx):
    await query_access(ctx)
    await ctx.access("institutions", ctx.iid, "O")
    ctx.parent = ctx.query


@endpoint("POST", "/advisory/{qid}/publish", model="PublishRequest", prepare=publisher)
async def publish(ctx, body):
    require_flow(advisor=True)
    row = ctx.query
    ctx.match(row)
    audit = await ctx.repo.get("institution_audits", row["audit_id"], lock=True)
    if row["status"] not in {"READY", "PARTIAL"} or not row["answer"] or not row["answer"]["answer"]["supported"] or audit["business_status"] not in {"FINALIZED", "SUPERSEDED"} or str(audit["reviewed_snapshot_id"]) != str(row["snapshot_id"]) or body["expected_context_hash"] != row["context_hash"] or body["expected_answer_hash"] != row["answer_hash"]:
        fail(409, "ADVISORY_CONTEXT_NOT_FINAL", "Publish only validated advice from this exact approved final snapshot.")
    saved = await ctx.repo.update("advisory_queries", row["id"], {"publication_state": "PUBLISHED", "published_by": ctx.actor["id"], "published_at": datetime.now(timezone.utc)}, bump=True)
    await ctx.repo.event(ctx.actor, "ADVICE_PUBLISHED", "advisory_queries", saved)
    ctx.revision = saved["input_revision"]
    return service.resource(saved)


@endpoint("POST", "/advisory/{qid}/withdraw", prepare=publisher)
async def withdraw(ctx, body):
    ctx.match(ctx.query)
    row = await ctx.repo.update("advisory_queries", ctx.query["id"], {"publication_state": "WITHDRAWN"}, bump=True)
    await ctx.repo.event(ctx.actor, "ADVICE_WITHDRAWN", "advisory_queries", row)
    ctx.revision = row["input_revision"]
    return service.resource(row)


@endpoint("POST", "/institution-audits/{aid}/scenarios", model="ScenarioRequest", prepare=__import__("routers.institution_audits", fromlist=["writable"]).writable)
async def scenario(ctx, body):
    from services.institution.scenario_service import create
    return await create(ctx, body)
