"""Atomic durable job acceptance and outbox creation."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.config import settings
from app.institution_errors import fail
from models.institution.common import digest

QUEUES = {"campus_mask": "tt_institution_raster", "campus_plot_generation": "tt_institution_raster", "campus_fusion": "tt_institution_raster", "institution_calculation": "tt_institution_accounting", "report_export": "tt_institution_exports", "evidence_check": "tt_institution_evidence", "advisory": "tt_institution_advisory", "knowledge_ingestion": "tt_institution_ingestion", "embedding_reindex": "tt_institution_ingestion", "account_deletion": "tt_institution_privacy"}
DEADLINES = {"tt_institution_raster": "INSTITUTION_RASTER_DEADLINE_SECONDS", "tt_institution_accounting": "INSTITUTION_ACCOUNTING_DEADLINE_SECONDS", "tt_institution_exports": "INSTITUTION_EXPORT_DEADLINE_SECONDS", "tt_institution_evidence": "INSTITUTION_UPLOAD_DEADLINE_SECONDS", "tt_institution_advisory": "INSTITUTION_ADVISORY_DEADLINE_SECONDS", "tt_institution_ingestion": "INSTITUTION_INGEST_DEADLINE_SECONDS", "tt_institution_privacy": "INSTITUTION_INGEST_DEADLINE_SECONDS"}


def accepted(job):
    result = {"job_id": str(job["id"]), "resource_id": str(job["resource_id"]), "input_revision": job["source_input_revision"], "status": job["status"]}
    if job["job_type"] == "advisory":
        result["query_id"] = result["resource_id"]
    return result


async def enqueue(ctx, job_type, resource, frozen, *, asset_type, job_id=None):
    input_hash = frozen.get("input_hash") or digest(frozen)
    existing = await ctx.repo.rows("SELECT * FROM processing_jobs WHERE domain='INSTITUTION' AND resource_id=:rid AND job_type=:type AND input_hash=:hash AND fixture_generation IS NOT DISTINCT FROM :generation ORDER BY created_at DESC LIMIT 1", {"rid": resource["id"], "type": job_type, "hash": input_hash, "generation": ctx.generation})
    if existing:
        return existing[0]
    queue = QUEUES[job_type]
    if ctx.iid and queue in {"tt_institution_raster", "tt_institution_advisory"}:
        types = [key for key, value in QUEUES.items() if value == queue]
        count = await ctx.repo.one("SELECT count(*) AS total FROM processing_jobs WHERE institution_id=:iid AND status IN ('QUEUED','RUNNING') AND job_type=ANY(CAST(:types AS text[]))", {"iid": ctx.iid, "types": types})
        if count["total"] >= (settings.INSTITUTION_RASTER_CONCURRENCY if queue == "tt_institution_raster" else settings.INSTITUTION_ADVISORY_CONCURRENCY):
            fail(429, "ACTIVE_JOB_LIMIT", "This institution already has its allowed active jobs.", retryable=True)
    job = await ctx.repo.insert("processing_jobs", {"id": job_id or str(uuid4()), "institution_id": ctx.iid, "domain": "INSTITUTION", "asset_type": asset_type, "resource_id": resource["id"], "requested_by": ctx.actor["id"], "job_type": job_type, "source_input_revision": resource["input_revision"], "input_hash": input_hash, "status": "QUEUED", "stage": "ACCEPTED", "progress": 0, "attempt": 0, "max_attempts": settings.INSTITUTION_JOB_MAX_RETRIES + 1, "frozen_input": frozen, "stage_outputs": {}, "provider_task_ids": {}, "retryable": False, "deadline_at": datetime.now(timezone.utc) + timedelta(seconds=getattr(settings, DEADLINES[queue])), "fixture_generation": ctx.generation})
    await ctx.repo.insert("job_outbox", {"job_id": job["id"], "payload_hash": input_hash, "queue_name": queue, "status": "PENDING", "attempts": 0, "next_attempt_at": datetime.now(timezone.utc)})
    await ctx.repo.event(ctx.actor, "JOB_ACCEPTED", "processing_jobs", job)
    return job
