"""Durable outbox dispatch, fenced leases, bounded retries and honest failures."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database_ssl import database_ssl_context
from app.institution_access import tenant_access, operator_access
from app.institution_errors import InstitutionError, fail
from models.institution.common import digest, normalize
from repositories.institution.base import Repository, columns
from tasks.institution_app import institution_app

RESOURCE_TABLE = {"institution_calculation": "institution_audits", "campus_mask": "campus_vegetation_masks", "campus_plot_generation": "campus_plot_sets", "campus_fusion": "campus_tree_audits", "evidence_check": "institution_audit_evidence", "report_export": "institution_exports", "advisory": "advisory_queries", "knowledge_ingestion": "knowledge_documents", "embedding_reindex": "knowledge_documents", "account_deletion": "institution_deletion_requests"}
EXTERNAL_UNKNOWN = {"advisory", "knowledge_ingestion", "embedding_reindex"}
logger = logging.getLogger(__name__)


def engine():
    if not settings.INSTITUTION_SCHEMA_ENABLED or not settings.DATABASE_URL:
        fail(503, "INSTITUTION_FLOW_DISABLED", "Institution schema is not commissioned.")
    # Worker loops are short-lived; never reuse an asyncpg pool across loops.
    return create_async_engine(settings.DATABASE_URL, poolclass=NullPool, echo=False, connect_args={"ssl": database_ssl_context(settings.DATABASE_SSL_CA_FILE), "command_timeout": 120, "server_settings": {"search_path": "public,extensions"}})


async def guarded(repo, job, *, lock=False):
    now = datetime.now(timezone.utc)
    if job["deadline_at"] <= now:
        fail(409, "JOB_DEADLINE_EXCEEDED", "The accepted operation exceeded its absolute deadline.")
    actor = await repo.one("SELECT * FROM users WHERE id=:id" + (" FOR SHARE" if lock and job["fixture_generation"] is not None else ""), {"id": job["requested_by"]})
    if not actor.get("firebase_uid") or not actor.get("phone_number"):
        fail(403, "MEMBERSHIP_REVOKED", "The requesting identity is no longer active.")
    if job["fixture_generation"] is not None:
        # Workers run in separate processes. Check the ordinary job row rather
        # than API-local demo markers; a reset deletes/replaces old job rows.
        current = await repo.get("processing_jobs", job["id"], required=False)
        if not settings.ENABLE_INSTITUTION_DEMO_ACCOUNTS or not current or current["fixture_generation"] != job["fixture_generation"]:
            fail(409, "DEMO_SESSION_RESET", "This job belongs to a superseded demo generation.")
    resource = await repo.get(RESOURCE_TABLE[job["job_type"]], job["resource_id"])
    if job["institution_id"]:
        await tenant_access(repo, actor, job["institution_id"], "W", own_pending=job["job_type"] == "evidence_check", lock=lock)
    elif job["job_type"] in {"knowledge_ingestion", "embedding_reindex"}:
        await operator_access(repo, actor, "KNOWLEDGE_REVIEW")
    elif job["job_type"] == "evidence_check":
        if str(resource["owner_user_id"]) != str(actor["id"]):
            fail(403, "MEMBERSHIP_REVOKED", "Evidence ownership changed.")
        if resource["parent_type"] in {"MODEL", "KNOWLEDGE", "FACTOR_SOURCE", "METHOD_SOURCE"}:
            capability = {"MODEL": "MODEL_REVIEW", "KNOWLEDGE": "KNOWLEDGE_REVIEW", "FACTOR_SOURCE": "FACTOR_REVIEW", "METHOD_SOURCE": "METHOD_REVIEW"}[resource["parent_type"]]
            await operator_access(repo, actor, capability)
    elif job["job_type"] == "account_deletion":
        await operator_access(repo, actor, "PRIVACY_OPERATIONS")
        if str(resource["user_id"]) == str(actor["id"]):
            fail(403, "REVIEWER_CONFLICT", "An independent privacy operator is required.")
    if job["job_type"] in {"campus_mask", "campus_plot_generation", "campus_fusion"}:
        campus = await repo.get("campuses", resource["campus_id"])
        if campus["control_status"] != "APPROVED" or campus["archived_at"] or campus["control_approved_hash"] != campus["control_input_hash"]:
            fail(403, "MEMBERSHIP_REVOKED", "Campus control changed before this operation finished.")
        if str(campus["current_boundary_id"]) != str(resource["boundary_id"]):
            fail(409, "INPUT_HASH_CHANGED", "Campus boundary changed before this operation finished.")
    if job["job_type"] == "institution_calculation" and (resource["input_hash"] != job["input_hash"] or resource["input_revision"] != job["source_input_revision"]):
        fail(409, "INPUT_HASH_CHANGED", "The source draft changed before calculation finished.")
    if lock:
        resource = await repo.get(RESOURCE_TABLE[job["job_type"]], job["resource_id"], lock=True)
    return actor, resource


async def claim(engine, jid, owner):
    async with engine.begin() as conn:
        repo = Repository(conn)
        job = await repo.get("processing_jobs", jid, lock=True)
        now = datetime.now(timezone.utc)
        if job["status"] != "QUEUED":
            return None
        if job["attempt"] >= job["max_attempts"] or job["deadline_at"] <= now:
            await repo.update("processing_jobs", jid, {"status": "FAILED", "stage": "DEADLINE", "finished_at": now, "retryable": False, "error": InstitutionError(409, "JOB_BUDGET_EXHAUSTED", "Job retry/deadline budget is exhausted.").payload()})
            return None
        return await repo.update("processing_jobs", jid, {"status": "RUNNING", "stage": "VALIDATING", "attempt": job["attempt"] + 1, "lease_owner": owner, "lease_expires_at": now + timedelta(seconds=90), "heartbeat_at": now, "started_at": job["started_at"] or now, "error": None, "retryable": False})


async def heartbeat(engine, jid, owner):
    while True:
        await asyncio.sleep(15)
        async with engine.begin() as conn:
            result = await conn.execute(text("UPDATE processing_jobs SET heartbeat_at=now(),lease_expires_at=LEAST(deadline_at,now()+interval '90 seconds') WHERE id=:id AND status='RUNNING' AND lease_owner=:owner AND deadline_at>now()"), {"id": jid, "owner": owner})
            if result.rowcount != 1:
                return


async def fenced(repo, jid, owner):
    job = await repo.get("processing_jobs", jid, lock=True)
    if job["status"] != "RUNNING" or job["lease_owner"] != owner or job["lease_expires_at"] <= datetime.now(timezone.utc):
        fail(409, "JOB_LEASE_LOST", "This worker no longer owns the operation.")
    return job


async def compute(engine, job):
    from services.institution import accounting_service, campus_tree_adapter, report_service
    from services.shared.evidence_service import verify_and_promote
    kind, frozen = job["job_type"], job["frozen_input"]
    if kind == "institution_calculation":
        return await asyncio.to_thread(accounting_service.calculate_inventory, frozen)
    if kind == "campus_mask":
        return await asyncio.to_thread(campus_tree_adapter.mask_geometry, frozen)
    if kind == "evidence_check":
        return await verify_and_promote(frozen["evidence"])
    if kind == "report_export":
        return await asyncio.to_thread(report_service.render, frozen["manifest"], frozen["format"])
    async with engine.begin() as conn:
        repo = Repository(conn)
        actor, resource = await guarded(repo, job)
        if kind == "campus_plot_generation":
            return await campus_tree_adapter.generate_plots(repo, frozen)
        if kind == "campus_fusion":
            return await campus_tree_adapter.compute_stock(repo, frozen)
        if kind == "advisory":
            from services.advisory.advisory_service import run
            output = await run(repo, frozen, resource)
            await guarded(repo, job, lock=True)
            await fenced(repo, job["id"], job["lease_owner"])
            return output
        if kind in {"knowledge_ingestion", "embedding_reindex"}:
            from services.advisory.ingestion_service import ingest
            if "managed_sources" in frozen:
                from services.advisory.managed_sources import ingest as ingest_managed
                output = await ingest_managed(repo, frozen, actor, job["id"])
            else:
                output = await ingest(repo, {"document_id": frozen["document"]["id"], "expected_artifact_hash": frozen["document"]["artifact_hash"], "ingestion_rules": frozen.get("ingestion_rules"), "job_id":str(job["id"])}, actor)
            await fenced(repo, job["id"], job["lease_owner"])
            await guarded(repo, job)
            return output
        if kind == "account_deletion":
            from services.institution.privacy_service import execute
            output = await execute(repo, job, resource)
            await guarded(repo, job, lock=True)
            await fenced(repo, job["id"], job["lease_owner"])
            return output
    fail(422, "UNSUPPORTED_JOB", "This job type is unsupported.")


async def publish(repo, job, actor, resource, output):
    from services.institution.base import case_for
    from services.institution.report_service import artifact
    from services.shared.sampling_service import measure
    if job["job_type"] == "knowledge_ingestion" and "managed_sources" in job["frozen_input"]:
        from services.advisory.managed_sources import activate
        return await activate(repo, job["frozen_input"], output)
    kind, frozen = job["job_type"], job["frozen_input"]
    if kind == "institution_calculation":
        result, errors = output
        sid = str(uuid4())
        result.update(id=sid, snapshot_id=sid)
        await repo.insert("institution_calculation_snapshots", {"id": sid, "institution_id": job["institution_id"], "audit_id": resource["id"], "source_input_revision": job["source_input_revision"], "input_hash": job["input_hash"], "result_hash": result["result_hash"], "processing_job_id": job["id"], "frozen_input": frozen, "result": result, "created_as_preview": resource["business_status"] == "DRAFT", "quality": {"flags": result["quality_flags"]}, "missing_data": errors})
        for line in result["calculation_lines"]:
            values = {key: value for key, value in line.items() if key in columns("institution_calculation_lines")}
            values.update(input_type="MITIGATION" if line["formula_key"].startswith("MIT-") else "EMISSION", result_unit=line["contribution_unit"], factor_id=line["factor_snapshots"][0]["id"] if len(line["factor_snapshots"]) == 1 else None)
            await repo.insert("institution_calculation_lines", {**values, "institution_id": job["institution_id"], "audit_id": resource["id"], "snapshot_id": sid})
        await repo.update("institution_audits", resource["id"], {"preview_snapshot_id": sid, "processing_status": "SUCCEEDED"})
        # A submitted case already freezes the inputs. Add the successful exact
        # calculation reference without changing its business input counter.
        cases = await repo.find("review_cases", {"resource_id": resource["id"], "status": "OPEN"})
        for case in cases:
            await repo.update("review_cases", case["id"], {"result_hash": result["result_hash"]})
        return sid
    if kind == "evidence_check":
        return (await repo.update("institution_audit_evidence", resource["id"], output))["id"]
    if kind == "report_export":
        data, mime = output
        item = await artifact(repo, job["institution_id"], "REPORT", resource["id"], data, mime, job_id=job["id"], snapshot_id=resource["snapshot_id"], draft=resource["draft_watermark"])
        await repo.update("institution_exports", resource["id"], {"status": "SUCCEEDED", "artifact_id": item["id"]})
        return item["id"]
    if kind == "campus_mask":
        geometry, parameters = output
        geometry, area = await measure(repo, geometry)
        item = await artifact(repo, job["institution_id"], "MASK", resource["id"], __import__("json").dumps({"type": "Feature", "geometry": geometry, "properties": parameters}).encode(), "application/geo+json", job_id=job["id"])
        resource = await repo.update("campus_vegetation_masks", resource["id"], {"eligible_geometry": geometry, "eligible_area_ha": area, "parameters": parameters, "coverage": {"source": parameters, "eligible_area_ha": normalize(area)}, "processing_status": "SUCCEEDED", "map_artifact_id": item["id"]})
        await case_for(repo, actor, "campus_vegetation_masks", resource, "MASK", {"resource": normalize(resource), "frozen_input": frozen})
        return resource["id"]
    if kind == "campus_plot_generation":
        plots, provenance = output
        for plot in plots:
            await repo.insert("campus_sampling_plots", {**plot, "institution_id": job["institution_id"], "campus_id": resource["campus_id"], "plot_set_id": resource["id"], "boundary_id": resource["boundary_id"], "mask_id": resource["mask_id"], "quality_policy_version": resource["quality_policy_version"], "review_status": "PENDING_REVIEW", "active_start": frozen["campus"]["created_at"][:10], "active_end": None})
        await case_for(repo, actor, "campus_plot_sets", resource, "PLOT_SET", {"plot_set": normalize(resource), "plots": normalize(plots), "provenance": provenance, "frozen_input": frozen})
        return resource["id"]
    if kind == "campus_fusion":
        result, grid = output
        item = await artifact(repo, job["institution_id"], "TREE_AUDIT", resource["id"], __import__("json").dumps(grid).encode(), "application/json", job_id=job["id"])
        hashed = digest({"input_hash": job["input_hash"], "result": result})
        resource = await repo.update("campus_tree_audits", resource["id"], {**result, "result_hash": hashed, "map_artifact_id": item["id"], "processing_status": "SUCCEEDED"})
        await case_for(repo, actor, "campus_tree_audits", resource, "TREE_AUDIT", {"result": normalize(resource), "frozen_input": frozen})
        return resource["id"]
    if kind == "advisory":
        await repo.update("advisory_queries", resource["id"], output)
        for recommendation in output["answer"]["answer"]["recommendations"]:
            await repo.insert("advisory_recommendations", {"institution_id": job["institution_id"], "query_id": resource["id"], "action_id": recommendation["action_id"], "structured_action": recommendation, "priority_class": recommendation["priority_class"], "priority": recommendation["priority"], "ranking_version": frozen["versions"]["ranking"], "rank_components": {"priority_class": recommendation["priority_class"]}, "tie_break": recommendation["action_id"], "citation_refs": recommendation["citations"], "scenario_id": recommendation["scenario_id"], "validation_flags": {"facts_checked": True, "citations_checked": True}})
        return resource["id"]
    return output


async def finish_failure(engine, jid, owner, exc):
    safe = exc if isinstance(exc, InstitutionError) else InstitutionError(503, "PROCESSING_FAILED", "The operation failed. Review the source and worker diagnostics.")
    # Log identifiers/types only: exception messages can contain private source
    # data or provider credentials. The public response remains bounded.
    logger.error("Institution job failed job_id=%s error_code=%s exception_type=%s", jid, safe.code, type(exc).__name__)
    async with engine.begin() as conn:
        repo = Repository(conn)
        job = await repo.get("processing_jobs", jid, lock=True)
        if job["lease_owner"] != owner or job["status"] != "RUNNING" or job["lease_expires_at"] <= datetime.now(timezone.utc):
            return
        access = safe.code in {"MEMBERSHIP_REVOKED", "PERMISSION_DENIED", "RESOURCE_NOT_FOUND", "DEMO_SESSION_RESET"}
        status = "BLOCKED_ACCESS" if access else "NEEDS_DATA" if safe.status == 422 else "FAILED"
        retryable = safe.retryable and job["attempt"] < job["max_attempts"] and job["deadline_at"] > datetime.now(timezone.utc)
        await repo.update("processing_jobs", jid, {"status": status, "stage": "FAILED", "error": safe.payload(), "retryable": retryable, "finished_at": datetime.now(timezone.utc), "lease_owner": None, "lease_expires_at": None})
    await reconcile_failure(engine, jid)


async def reconcile_failure(engine, jid):
    """Project committed terminal failures using tenant/resource/job lock order.

    Watchdog locks never extend to business resources. Reconciliation rechecks
    the job after acquiring the same locks as publish and ignores old jobs.
    """
    async with engine.begin() as conn:
        repo = Repository(conn)
        job = await repo.get("processing_jobs", jid)
        if job["status"] not in {"FAILED", "NEEDS_DATA", "BLOCKED_ACCESS"}:
            return
        if job["institution_id"]:
            await repo.get("institutions", job["institution_id"], lock=True)
        table = RESOURCE_TABLE[job["job_type"]]
        resource = await repo.get(table, job["resource_id"], lock=True, required=False)
        job = await repo.get("processing_jobs", jid, lock=True)
        if job["status"] not in {"FAILED", "NEEDS_DATA", "BLOCKED_ACCESS"}:
            return
        await repo.update("processing_jobs", jid, {"stage": "FAILURE_RECONCILED"})
        if resource is None:
            return
        latest = await repo.one("SELECT id FROM processing_jobs WHERE domain='INSTITUTION' AND resource_id=:rid AND job_type=:kind ORDER BY created_at DESC,id DESC LIMIT 1", {"rid": job["resource_id"], "kind": job["job_type"]})
        if str(latest["id"]) != str(jid) or (table == "institution_audits" and resource["input_hash"] != job["input_hash"]):
            return
        status, access = job["status"], job["status"] == "BLOCKED_ACCESS"
        values = {"processing_status": status} if "processing_status" in columns(table) else {"status": "BLOCKED_ACCESS" if access else "FAILED"} if table in {"advisory_queries", "institution_exports"} else {}
        error = job.get("error") or {}
        if table == "institution_audit_evidence" and error.get("code") in {"INVALID_FILE", "FILE_HASH_MISMATCH"}:
            values = {"state": "REJECTED", "rejection_reason": error["message"]}
        if values:
            await repo.update(table, job["resource_id"], values)
        if table == "institution_deletion_requests":
            await repo.update(table, job["resource_id"], {"status": "FAILED_RETRYABLE" if job["retryable"] else "NEEDS_ACTION"})


async def execute_job(jid):
    db, owner = engine(), str(uuid4())
    pulse = None
    try:
        job = await claim(db, jid, owner)
        if not job:
            await reconcile_failure(db, jid)
            return
        pulse = asyncio.create_task(heartbeat(db, jid, owner))
        async with db.begin() as conn:
            repo = Repository(conn)
            await guarded(repo, job, lock=True)
            await fenced(repo, jid, owner)
            table = RESOURCE_TABLE[job["job_type"]]
            if "processing_status" in columns(table):
                await repo.update(table, job["resource_id"], {"processing_status": "RUNNING"})
            elif table in {"advisory_queries", "institution_exports"}:
                await repo.update(table, job["resource_id"], {"status": "RUNNING"})
            await repo.update("processing_jobs", jid, {"stage": "COMPUTING", "stage_outputs": {"provider_started": job["job_type"] in EXTERNAL_UNKNOWN, "operation_key": digest({"job_id": jid, "input_hash": job["input_hash"]})}})
        timeout = max(0.001, (job["deadline_at"] - datetime.now(timezone.utc)).total_seconds())
        from services.advisory.provider_operations import current_operation
        operation_token = current_operation.set((db,job["id"],owner))
        try:
            output = await asyncio.wait_for(compute(db, job), timeout=timeout)
        finally:
            current_operation.reset(operation_token)
        async with db.begin() as conn:
            repo = Repository(conn)
            # Tenant first, job second: membership changes serialize with publish.
            actor, resource = await guarded(repo, job, lock=True)
            await fenced(repo, jid, owner)
            output_ref = await publish(repo, job, actor, resource, output)
            await repo.update("processing_jobs", jid, {"status": "SUCCEEDED", "stage": "COMPLETE", "progress": 100, "output_ref": output_ref, "finished_at": datetime.now(timezone.utc), "lease_owner": None, "lease_expires_at": None})
            await repo.event(actor, "JOB_SUCCEEDED", "processing_jobs", {**job, "output_ref": output_ref})
    except BaseException as exc:
        if not isinstance(exc, asyncio.CancelledError):
            await finish_failure(db, jid, owner, exc)
        raise_if_interrupt = isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError))
        if raise_if_interrupt:
            raise
    finally:
        if pulse:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
        await db.dispose()


async def dispatch():
    db = engine()
    try:
        async with db.begin() as conn:
            repo = Repository(conn)
            rows = await repo.rows("SELECT * FROM job_outbox WHERE status='PENDING' AND (next_attempt_at IS NULL OR next_attempt_at<=now()) ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 50")
            for row in rows:
                try:
                    await asyncio.to_thread(institution_app.send_task, "institution.execute", args=[str(row["job_id"])], queue=row["queue_name"], task_id=str(row["job_id"]))
                except Exception:
                    await repo.update("job_outbox", row["id"], {"attempts": row["attempts"]+1, "next_attempt_at": datetime.now(timezone.utc)+timedelta(seconds=min(300, 2 ** min(row["attempts"], 8)))})
                    # One bounded failed send is enough to establish a broker
                    # outage; release the batch locks so watchdog work can run.
                    break
                else:
                    await repo.update("job_outbox", row["id"], {"status": "DISPATCHED", "dispatched_at": datetime.now(timezone.utc), "attempts": row["attempts"]+1})
    finally:
        await db.dispose()


async def watchdog():
    db = engine()
    terminal = []
    try:
        async with db.begin() as conn:
            repo = Repository(conn)
            rows = await repo.rows("SELECT * FROM processing_jobs WHERE domain='INSTITUTION' AND ((status='RUNNING' AND lease_expires_at<now()) OR (status IN ('QUEUED','RUNNING') AND deadline_at<=now())) FOR UPDATE SKIP LOCKED LIMIT 100")
            now = datetime.now(timezone.utc)
            for job in rows:
                unresolved = await repo.rows("SELECT id FROM institution_provider_operations WHERE job_id=:id AND status IN ('STARTED','UNKNOWN') LIMIT 1", {"id":job["id"]})
                unknown = bool(unresolved)
                retry = not unknown and job["deadline_at"] > now and job["attempt"] < job["max_attempts"]
                code = "EXTERNAL_OUTCOME_UNKNOWN" if unknown else "JOB_LEASE_EXPIRED"
                await repo.update("processing_jobs", job["id"], {"status": "QUEUED" if retry else "FAILED", "stage": "RECOVERY", "retryable": False, "lease_owner": None, "lease_expires_at": None, "error": InstitutionError(503, code, "The previous worker did not confirm completion.").payload(), "finished_at": None if retry else now})
                if retry:
                    await conn.execute(text("UPDATE job_outbox SET status='PENDING',next_attempt_at=now() WHERE job_id=:id"), {"id": job["id"]})
                else:
                    terminal.append(job["id"])
            await conn.execute(text("UPDATE idempotency_records SET receipt_state='TOMBSTONE',response=NULL,secret_ciphertext=NULL,secret_expires_at=NULL,encryption_key_id=NULL,updated_at=now() WHERE receipt_state='FULL' AND expires_at<=now()"))
        # Also repair a crash between committing a terminal job and projecting
        # its resource. Markers keep old failures from starving later batches.
        async with db.begin() as conn:
            pending = await Repository(conn).rows("SELECT id FROM processing_jobs WHERE domain='INSTITUTION' AND status IN ('FAILED','NEEDS_DATA','BLOCKED_ACCESS') AND stage<>'FAILURE_RECONCILED' ORDER BY finished_at,id LIMIT 100")
            terminal.extend(row["id"] for row in pending)
        for jid in dict.fromkeys(terminal):
            await reconcile_failure(db, jid)
    finally:
        await db.dispose()


@institution_app.task(name="institution.execute", soft_time_limit=21630, time_limit=21660)
def execute(jid):
    asyncio.run(execute_job(jid))


@institution_app.task(name="institution.dispatch")
def dispatch_task():
    if settings.INSTITUTION_SCHEMA_ENABLED:
        asyncio.run(dispatch())


@institution_app.task(name="institution.watchdog")
def watchdog_task():
    if settings.INSTITUTION_SCHEMA_ENABLED:
        asyncio.run(watchdog())


