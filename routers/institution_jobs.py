"""Tenant-authorized status and budget-preserving manual recovery."""
from datetime import datetime, timezone
from sqlalchemy import text

from app.institution_context import endpoint
from app.institution_access import operator_access, require_flow
from app.institution_errors import fail
from tasks.institution_tasks import RESOURCE_TABLE, guarded


async def job_access(ctx):
    row = await ctx.repo.get("processing_jobs", ctx.request.path_params["jid"])
    if row["domain"] != "INSTITUTION":
        fail(404, "RESOURCE_NOT_FOUND", "Job is unavailable.")
    if row["institution_id"]:
        try:
            await ctx.access("institutions", row["institution_id"], "W")
        except __import__("app.institution_errors",fromlist=["InstitutionError"]).InstitutionError:
            if ctx.request.method != "GET":
                raise
            from services.institution.reviewer_access import claimed_resource
            await claimed_resource(ctx,row["institution_id"],row["resource_id"])
    elif row["job_type"] in {"knowledge_ingestion", "embedding_reindex"}:
        await operator_access(ctx.repo, ctx.actor, "KNOWLEDGE_REVIEW")
    elif str(row["requested_by"]) != str(ctx.actor["id"]):
        fail(404, "RESOURCE_NOT_FOUND", "Job is unavailable.")
    if getattr(ctx, "lock_mode", False):
        # Match worker publish/reconciliation ordering, including global jobs.
        await guarded(ctx.repo, row, lock=True)
        row = await ctx.repo.get("processing_jobs", row["id"], lock=True)
    ctx.job = row


def resource(row):
    return {"id": str(row["id"]), "type": row["job_type"], "resource_id": str(row["resource_id"]), "domain": row["domain"], "institution_id": str(row["institution_id"]) if row["institution_id"] else None, "input_revision": row["source_input_revision"], "input_hash": row["input_hash"], "status": row["status"], "stage": row["stage"], "progress": row["progress"], "attempt": row["attempt"], "retryable": row["retryable"], "error": row["error"], "output_ref": str(row["output_ref"]) if row["output_ref"] else None, "started_at": row["started_at"], "finished_at": row["finished_at"], "poll_after_seconds": 5 if row["status"] in {"QUEUED", "RUNNING"} else 0}


@endpoint("GET", "/jobs/{jid}", prepare=job_access)
async def get_job(ctx, _):
    ctx.revision = ctx.job["input_revision"]
    return resource(ctx.job)


@endpoint("POST", "/jobs/{jid}/retry", model="JobRetry", status=202, prepare=job_access)
async def retry(ctx, body):
    require_flow(advisor=ctx.job["job_type"] == "advisory")
    row = ctx.job
    ctx.match(row)
    if not row["retryable"] or row["status"] != "FAILED" or row["attempt"] >= row["max_attempts"] or row["deadline_at"] <= datetime.now(timezone.utc) or row["input_hash"] != body["expected_input_hash"]:
        fail(409, "JOB_NOT_RETRYABLE", "This operation has no remaining safe retry budget.")
    if (row.get("error") or {}).get("code") == "EXTERNAL_OUTCOME_UNKNOWN":
        fail(409, "EXTERNAL_OUTCOME_UNKNOWN", "An unknown provider outcome cannot be resubmitted.")
    await guarded(ctx.repo, row, lock=True)
    saved = await ctx.repo.update("processing_jobs", row["id"], {"status": "QUEUED", "stage": "RECOVERY", "retryable": False, "finished_at": None}, bump=True)
    await ctx.repo.conn.execute(text("UPDATE job_outbox SET status='PENDING',next_attempt_at=now() WHERE job_id=:id"), {"id": row["id"]})
    await ctx.repo.event(ctx.actor, "JOB_RETRY_REQUESTED", "processing_jobs", saved, reason=body["reason"])
    ctx.revision = saved["input_revision"]
    from services.institution.job_service import accepted
    return accepted(saved)
