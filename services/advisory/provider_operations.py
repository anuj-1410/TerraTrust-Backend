"""Durable provider checkpoints, including unknown acceptance after a crash."""
from contextvars import ContextVar
from datetime import datetime, timezone
import json
from uuid import uuid4

from sqlalchemy import text

from app.institution_errors import InstitutionError, fail
from models.institution.common import digest
from repositories.institution.base import Repository

current_operation = ContextVar("institution_provider_job", default=None)


async def invoke(operation, model, payload, send):
    context = current_operation.get()
    if context is None:
        return await send()
    engine, job_id, lease_owner = context
    request_hash = digest({"operation":operation,"model":model,"payload":payload})
    async with engine.begin() as conn:
        repo = Repository(conn)
        await conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),{"scope":str(job_id)+request_hash})
        job = await repo.get("processing_jobs",job_id)
        if job["status"] != "RUNNING" or job["lease_owner"] != lease_owner or job["deadline_at"] <= datetime.now(timezone.utc) or job["lease_expires_at"] <= datetime.now(timezone.utc):
            fail(409,"JOB_LEASE_LOST","This worker no longer owns provider work.")
        row = await repo.one("SELECT * FROM institution_provider_operations WHERE job_id=:job AND request_hash=:hash FOR UPDATE",{"job":job_id,"hash":request_hash},required=False)
        if row and row["status"] == "SUCCEEDED":
            return row["response"]
        if row and row["status"] in {"STARTED","UNKNOWN"}:
            fail(503,"EXTERNAL_OUTCOME_UNKNOWN","This provider operation may already have been accepted; no automatic resubmission is permitted.")
        if row:
            operation_id = row["id"]
            await conn.execute(text("UPDATE institution_provider_operations SET status='STARTED',updated_at=now() WHERE id=:id"),{"id":operation_id})
        else:
            operation_id = str(uuid4())
            await conn.execute(text("INSERT INTO institution_provider_operations(id,job_id,request_hash,operation,model,status) VALUES(:id,:job,:hash,:operation,:model,'STARTED')"),{"id":operation_id,"job":job_id,"hash":request_hash,"operation":operation,"model":model})
    try:
        response = await send()
    except InstitutionError as exc:
        status = "REJECTED" if exc.code in {"ADVISOR_QUOTA_EXCEEDED","ADVISOR_UNAVAILABLE","ADVISOR_PROVIDER_REJECTED"} else "UNKNOWN"
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE institution_provider_operations SET status=:state,error_code=:error,updated_at=now() WHERE id=:id"),{"state":status,"error":exc.code,"id":operation_id})
        raise
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE institution_provider_operations SET status='SUCCEEDED',response=CAST(:response AS jsonb),updated_at=now() WHERE id=:id"),{"id":operation_id,"response":json.dumps(response,ensure_ascii=False)})
    return response
