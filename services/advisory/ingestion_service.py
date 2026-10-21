"""Public, validated-byte ingestion. No tenant file or arbitrary URL fetching."""
import hashlib
import io
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import text
from pypdf import PdfReader
from app.config import settings

from app.institution_errors import fail
from models.institution.common import digest, normalize
from repositories.institution.base import Repository
from services.advisory.gemini_provider import embed
from services.shared.evidence_service import read_bytes


def chunks(pages, size, overlap):
    """UTF8_BYTES_V1: exact byte tokenizer with safe Unicode boundaries.

    This commissioned conservative tokenizer bounds tokens by UTF-8 bytes; it
    makes no claim to implement Gemini's proprietary tokenizer.
    """
    if not 0 <= overlap < size <= 800:
        raise ValueError("Invalid commissioned chunk bounds.")
    for page, body in pages:
        chars, current, nbytes = list(body), [], 0
        position = 0
        while position < len(chars):
            start = position
            current, nbytes = [], 0
            while position < len(chars) and nbytes + len(chars[position].encode()) <= size:
                current.append(chars[position]); nbytes += len(chars[position].encode()); position += 1
            if position == start:
                fail(422, "CHUNK_BOUND_TOO_SMALL", "The commissioned byte bound cannot hold a source character.")
            value = "".join(current).strip()
            if value:
                yield page, value
            if position == len(chars):
                break
            retained, count = position, 0
            while retained > start and count + len(chars[retained-1].encode()) <= overlap:
                retained -= 1; count += len(chars[retained].encode())
            position = max(start + 1, retained)


async def ingest(repo, frozen, actor):
    document = await repo.get("knowledge_documents", frozen["document_id"], lock=True)
    if document["status"] != "DRAFT" or document["artifact_hash"] != frozen["expected_artifact_hash"]:
        fail(409,"ARTIFACT_HASH_CHANGED","The accepted unpublished document changed before ingestion.")
    config = document["config"]
    from services.advisory.ingestion_rules import retrieval_config
    from app.institution_rules import RETRIEVAL_RULES_ID
    rules = frozen.get("ingestion_rules")
    if not rules or rules.get("id") != RETRIEVAL_RULES_ID or rules.get("artifact_hash") != digest(rules.get("config", {})):
        fail(409, "INGESTION_CONFIG_CHANGED", "Reindex this source with the current application ingestion rules.")
    pc = rules["config"]
    if not document["source_evidence_id"] and config["source_type"] == "URL":
        from services.advisory.public_source_service import fetch
        from services.shared.evidence_service import write_bytes, verify_and_promote
        from services.institution.registry_service import registry_hash
        data = await asyncio.to_thread(fetch,config["source_url"],set(pc.get("allowed_source_hosts",[])))
        hashed, eid = hashlib.sha256(data).hexdigest(),str(uuid4())
        quarantine = f"quarantine/knowledge/{eid}/{hashed}"
        await write_bytes(settings.PRIVATE_STORAGE_BUCKET,quarantine,data,"application/pdf")
        candidate = await repo.insert("institution_audit_evidence",{"id":eid,"institution_id":None,"owner_user_id":actor["id"],"parent_type":"KNOWLEDGE","parent_id":document["id"],"file_name":"public-source.pdf","mime_type":"application/pdf","private_bucket":settings.PRIVATE_STORAGE_BUCKET,"object_key":quarantine,"expected_sha256":hashed,"size_bytes":len(data),"state":"CHECKING","expires_at":datetime.now(timezone.utc)+timedelta(minutes=5),"publication_state":"PUBLIC_APPROVED"})
        candidate = await repo.update("institution_audit_evidence",eid,await verify_and_promote(candidate))
        config = {**config,"source_byte_hash":hashed,"evidence_id":eid}
        document = await repo.update("knowledge_documents",document["id"],{"source_evidence_id":eid,"config":config,"artifact_hash":registry_hash({**document,"config":config,"source_evidence_id":eid},hashed)},bump=True)
    evidence = await repo.get("institution_audit_evidence", document["source_evidence_id"])
    if evidence["parent_type"] != "KNOWLEDGE" or str(evidence["parent_id"]) != str(document["id"]) or evidence["institution_id"] is not None or evidence["state"] != "READY" or evidence["bytes_deleted_at"]:
        fail(422, "SOURCE_BYTES_REQUIRED", "Ingest only the exact validated public knowledge source.")
    data = await read_bytes(evidence["private_bucket"], evidence["object_key"])
    checksum = hashlib.sha256(data).hexdigest()
    if checksum != evidence["byte_hash"] or checksum != config["source_byte_hash"]:
        fail(422, "SOURCE_HASH_CHANGED", "Reviewed source bytes do not match the configured checksum.")
    if evidence["mime_type"] != "application/pdf":
        fail(422, "UNSUPPORTED_SOURCE_FORMAT", "The reviewed public-document parser currently supports PDF.")
    pages = [(i+1, page.extract_text() or "") for i, page in enumerate(PdfReader(io.BytesIO(data), strict=True).pages)]
    if not any(value.strip() for _, value in pages):
        fail(422, "DOCUMENT_NEEDS_OCR", "This source requires a separately reviewed OCR pipeline.")
    corpus_id = str(uuid4())
    prior_ids = pc.get("base_document_ids", [])
    for did in prior_ids:
        prior = await repo.get("knowledge_documents", did)
        if prior["status"] != "ACTIVE":
            fail(422, "CORPUS_SOURCE_INACTIVE", "All base corpus documents must be active.")
    document_ids = sorted(set(map(str, prior_ids)) | {str(document["id"])})
    embedding = pc["embedding"]
    if embedding["provider"] != "GEMINI":
        fail(503, "EMBEDDING_NOT_CONFIGURED", "Commission the Gemini embedding provider.")
    from services.advisory.provider_policy import authorized
    await authorized(repo, [embedding["model"]])
    candidate = await repo.insert("knowledge_corpus_versions", {"id": corpus_id, "manifest": {"document_id": str(document["id"]), "document_ids": document_ids, "processing_status": "RUNNING"}, "manifest_hash": digest(document_ids), "ingestion_config": {"tokenizer": pc["tokenizer"], "chunk_size": pc["chunk_size"], "overlap": pc["overlap"], "policy_id": str(rules["id"]), "policy_hash": rules["artifact_hash"]}, "embedding_config": embedding, "retrieval_config": {}, "pipeline_version": pc["pipeline_version"], "status": "DRAFT", "author_id": actor["id"]})
    prior_chunks = await repo.find("knowledge_chunks", {"document_id": document["id"]})
    if prior_chunks and any(chunk["metadata"].get("source_checksum") != checksum or chunk["metadata"].get("ingestion_policy_hash") != rules["artifact_hash"] for chunk in prior_chunks):
        fail(409,"NEW_DOCUMENT_VERSION_REQUIRED","Changed source bytes require a new immutable document version before reindexing.")
    if not prior_chunks:
        for index, (page, content) in enumerate(chunks(pages, pc["chunk_size"], pc["overlap"])):
            row = await repo.insert("knowledge_chunks", {"document_id": document["id"], "document_version": document["version"], "chunk_index": index, "text": content, "page_start": page, "page_end": page, "section": None, "metadata": {"tokenizer": pc["tokenizer"], "source_checksum": checksum, "ingestion_policy_hash": rules["artifact_hash"]}, "text_hash": hashlib.sha256(content.encode()).hexdigest()})
            await repo.conn.execute(text("UPDATE knowledge_chunks SET lexical_search_vector=to_tsvector('simple',text) WHERE id=:id"), {"id": row["id"]})
    all_chunks = await repo.rows("SELECT * FROM knowledge_chunks WHERE document_id=ANY(CAST(:ids AS uuid[])) ORDER BY document_id,chunk_index", {"ids": document_ids})
    for chunk in all_chunks:
        vector = await embed(chunk["text"], model=embedding["model"], dimension=embedding["dimension"])
        # Vector is server-produced finite floats; text parameter remains bound.
        await repo.conn.execute(text("INSERT INTO knowledge_embeddings(chunk_id,corpus_version,embedding_model,embedding_version,dimension,embedding) VALUES(:chunk,:corpus,:model,:version,:dimension,CAST(:embedding AS vector))"), {"chunk": chunk["id"], "corpus": corpus_id, "model": embedding["model"], "version": embedding["version"], "dimension": embedding["dimension"], "embedding": "[" + ",".join(map(str, vector)) + "]"})
    manifest = {"document_id": str(document["id"]), "document_ids": document_ids, "processing_status": "SUCCEEDED", "source_artifact_hash":document["artifact_hash"],"source_checksum":checksum,"processing_job_id":frozen["job_id"], "chunks": [{"id": str(c["id"]), "hash": c["text_hash"]} for c in all_chunks]}
    await repo.update("knowledge_corpus_versions", corpus_id, {"manifest": manifest, "manifest_hash": digest(manifest), "retrieval_config": retrieval_config(all_chunks)})
    await repo.update("knowledge_documents", document["id"], {"checksum": checksum, "extraction_quality": {"parser": "PYPDF_STRICT_V1", "pages": len(pages), "ocr": False}})
    return corpus_id
