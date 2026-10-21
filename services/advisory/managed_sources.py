"""Index administrator-listed public PDFs without manual UUID/checksum entry."""

import asyncio
import hashlib
import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from pypdf import PdfReader
from sqlalchemy import text

from app.config import settings
from app.institution_access import operator_access
from app.institution_errors import fail
from app.institution_rules import RETRIEVAL_RULES_ID
from models.institution.common import digest, normalize
from services.advisory.ingestion_rules import configuration, retrieval_config
from services.advisory.ingestion_service import chunks
from services.advisory.gemini_provider import embed
from services.institution.job_service import enqueue
from services.institution.registry_service import registry_hash
from services.shared.evidence_service import read_bytes, write_bytes, inspect_bytes


def extract_pages(data):
    inspect_bytes(data, "application/pdf", "guidance.pdf")
    pages, characters = [], 0
    for index, page in enumerate(PdfReader(io.BytesIO(data), strict=True).pages):
        content = page.extract_text() or ""
        characters += len(content)
        if characters > 4_000_000:
            fail(
                413,
                "SOURCE_TEXT_TOO_LARGE",
                "One public PDF may contain at most four million extracted characters.",
            )
        pages.append((index + 1, content))
    return pages


def source_snapshot(rows):
    # Exclude wall-clock timestamps; SQL revision is advanced for every edit.
    return normalize(
        sorted(
            [
                {
                    key: value
                    for key, value in row.items()
                    if key not in {"created_at", "updated_at"}
                }
                for row in rows
            ],
            key=lambda row: row["logical_key"],
        )
    )


async def sources(repo):
    return await repo.find("institution_knowledge_sources", {"enabled": True})


async def prepare(repo, actor):
    await operator_access(repo, actor, "KNOWLEDGE_REVIEW")
    await repo.conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended('tt:managed-knowledge',0))")
    )
    running = await repo.rows(
        "SELECT id FROM processing_jobs WHERE job_type='knowledge_ingestion' AND frozen_input ? 'managed_sources' AND status IN ('QUEUED','RUNNING') LIMIT 1"
    )
    if running:
        fail(
            409,
            "INGESTION_ALREADY_RUNNING",
            "Wait for the current PDF indexing job to finish.",
        )
    rows = await sources(repo)
    if not rows:
        fail(
            422,
            "NO_KNOWLEDGE_SOURCES",
            "Enable at least one licensed public PDF in institution_knowledge_sources.",
        )
    if len(rows) > 25:
        fail(
            413,
            "TOO_MANY_SOURCES",
            "The initial public corpus supports at most 25 PDFs per indexing job.",
        )
    frozen_sources = source_snapshot(rows)
    files = []
    for row in rows:
        if not row["public_licensed"] or not row["license"].strip():
            fail(
                422,
                "PUBLIC_SOURCE_REQUIRED",
                "Index only licensed public guidance; tenant/private documents must never be listed.",
            )
        if not row["object_key"].startswith("knowledge/") or ".." in row["object_key"]:
            fail(
                422,
                "INVALID_SOURCE_PATH",
                "PDFs must use a knowledge/ object path in institution-evidence.",
            )
        data = await read_bytes(settings.PRIVATE_STORAGE_BUCKET, row["object_key"])
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    inspect_bytes, data, "application/pdf", "guidance.pdf"
                ),
                timeout=30,
            )
        except Exception:
            fail(
                422,
                "INVALID_SOURCE_PDF",
                "One configured source is not a supported searchable public PDF.",
            )
        files.append((row, data, hashlib.sha256(data).hexdigest()))
        if sum(len(item[1]) for item in files) > 100 * 1024 * 1024:
            fail(
                413,
                "CORPUS_TOO_LARGE",
                "One public corpus indexing job supports at most 100 MiB of source PDFs.",
            )
    rules = await configuration(repo)
    from services.advisory.provider_policy import authorized

    await authorized(repo, [rules["config"]["embedding"]["model"]])
    # Managed indexing replaces its complete source set; disabled rows disappear
    # on successful reindex. No stale previously-active document is inherited.
    rules["config"]["base_document_ids"] = []
    rules["artifact_hash"] = digest(rules["config"])
    source_hash = digest(
        {
            "sources": frozen_sources,
            "bytes": {row["logical_key"]: checksum for row, _, checksum in files},
            "rules": rules,
        }
    )
    from services.advisory.corpus_service import current

    previous = await current(repo, required=False)
    if previous and previous["manifest"].get("managed_source_hash") == source_hash:
        return {"status": "UNCHANGED", "job_id": None}
    documents = []
    for row, data, checksum in files:
        from services.institution.runtime_configuration import next_version

        version = await next_version(
            repo, "knowledge_documents", "DB:" + row["logical_key"]
        )
        did, eid = str(uuid4()), str(uuid4())
        key = f"verified/{eid}/{checksum}"
        # The immutable copy decouples queued work from later Storage edits.
        await write_bytes(settings.PRIVATE_STORAGE_BUCKET, key, data, "application/pdf")
        config = {
            "source_type": "UPLOAD",
            "source_url": row["source_url"],
            "evidence_id": eid,
            "topic_codes": row["topic_codes"],
            "language": row["language"],
            "visibility": "PUBLIC_APPROVED",
            "supersedes_document_id": None,
            "extraction_notes": "Public PDF configured by the Supabase administrator.",
            "source_byte_hash": checksum,
            "ingestion_config_version": RETRIEVAL_RULES_ID,
        }
        from services.institution.factor_service import validate_config

        config = normalize(validate_config("KNOWLEDGE", config))
        values = {
            "id": did,
            "logical_key": "DB:" + row["logical_key"],
            "version": version,
            "schema_version": 1,
            "title": row["title"],
            "publisher": row["publisher"],
            "source_version": checksum[:16],
            "license": row["license"],
            "source_url": row["source_url"],
            "source_evidence_id": eid,
            "geography_codes": row["geography_codes"],
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "applicability_notes": "Administrator-designated public source; no independent source review is asserted.",
            "config": config,
            "status": "DRAFT",
            "author_id": actor["id"],
        }
        # Insert the parent without its evidence FK, then link the owned evidence.
        document = await repo.insert(
            "knowledge_documents",
            {
                **values,
                "source_evidence_id": None,
                "artifact_hash": registry_hash(values, checksum),
            },
        )
        await repo.insert(
            "institution_audit_evidence",
            {
                "id": eid,
                "institution_id": None,
                "owner_user_id": actor["id"],
                "parent_type": "KNOWLEDGE",
                "parent_id": did,
                "file_name": "guidance.pdf",
                "mime_type": "application/pdf",
                "private_bucket": settings.PRIVATE_STORAGE_BUCKET,
                "object_key": key,
                "expected_sha256": checksum,
                "byte_hash": checksum,
                "size_bytes": len(data),
                "actual_size_bytes": len(data),
                "state": "READY",
                "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
                "publication_state": "PUBLIC_APPROVED",
            },
        )
        document = await repo.update(
            "knowledge_documents", did, {"source_evidence_id": eid}
        )
        documents.append(normalize(document))
    frozen = {
        "managed_sources": frozen_sources,
        "managed_source_hash": source_hash,
        "documents": documents,
        "document": documents[0],
        "ingestion_rules": rules,
        "input_hash": source_hash,
    }
    ctx = SimpleNamespace(repo=repo, actor=actor, iid=None, generation=None)
    job = await enqueue(
        ctx, "knowledge_ingestion", documents[0], frozen, asset_type="KNOWLEDGE"
    )
    return {"status": job["status"], "job_id": str(job["id"])}


async def unchanged(repo, frozen):
    if source_snapshot(await sources(repo)) != frozen["managed_sources"]:
        fail(
            409,
            "KNOWLEDGE_SOURCES_CHANGED",
            "The PDF source table changed during indexing; sync the current sources again.",
        )


async def ingest(repo, frozen, actor, job_id):
    await unchanged(repo, frozen)
    rules = frozen["ingestion_rules"]
    if rules.get("id") != RETRIEVAL_RULES_ID or rules["artifact_hash"] != digest(
        rules["config"]
    ):
        fail(
            409,
            "INGESTION_CONFIG_CHANGED",
            "Index again using the current ingestion rules.",
        )
    pc, all_chunks, document_ids = rules["config"], [], []
    for expected in frozen["documents"]:
        doc = await repo.get("knowledge_documents", expected["id"], lock=True)
        if (
            doc["status"] != "DRAFT"
            or doc["artifact_hash"] != expected["artifact_hash"]
        ):
            fail(
                409,
                "ARTIFACT_HASH_CHANGED",
                "A prepared PDF version changed during indexing.",
            )
        evidence = await repo.get(
            "institution_audit_evidence", doc["source_evidence_id"]
        )
        if (
            evidence["state"] != "READY"
            or evidence["bytes_deleted_at"]
            or str(evidence["parent_id"]) != str(doc["id"])
            or evidence["parent_type"] != "KNOWLEDGE"
            or evidence["institution_id"] is not None
        ):
            fail(
                422, "SOURCE_BYTES_REQUIRED", "The prepared public PDF is unavailable."
            )
        data = await read_bytes(evidence["private_bucket"], evidence["object_key"])
        checksum = hashlib.sha256(data).hexdigest()
        if (
            checksum != evidence["byte_hash"]
            or checksum != doc["config"]["source_byte_hash"]
        ):
            fail(422, "SOURCE_HASH_CHANGED", "The prepared public PDF changed.")
        pages = await asyncio.wait_for(
            asyncio.to_thread(extract_pages, data), timeout=60
        )
        if not any(value.strip() for _, value in pages):
            fail(
                422,
                "DOCUMENT_NEEDS_OCR",
                "Use a searchable PDF; this PDF contains no extractable text.",
            )
        prior = await repo.find("knowledge_chunks", {"document_id": doc["id"]})
        if prior:
            if any(
                row["metadata"].get("source_checksum") != checksum
                or row["metadata"].get("ingestion_policy_hash")
                != rules["artifact_hash"]
                for row in prior
            ):
                fail(
                    409,
                    "NEW_DOCUMENT_VERSION_REQUIRED",
                    "Prepare a new version for changed parser inputs.",
                )
            all_chunks.extend(prior)
        else:
            for index, (page, content) in enumerate(
                chunks(pages, pc["chunk_size"], pc["overlap"])
            ):
                chunk = await repo.insert(
                    "knowledge_chunks",
                    {
                        "document_id": doc["id"],
                        "document_version": doc["version"],
                        "chunk_index": index,
                        "text": content,
                        "page_start": page,
                        "page_end": page,
                        "section": None,
                        "metadata": {
                            "tokenizer": pc["tokenizer"],
                            "source_checksum": checksum,
                            "ingestion_policy_hash": rules["artifact_hash"],
                        },
                        "text_hash": hashlib.sha256(content.encode()).hexdigest(),
                    },
                )
                await repo.conn.execute(
                    text(
                        "UPDATE knowledge_chunks SET lexical_search_vector=to_tsvector('simple',text) WHERE id=:id"
                    ),
                    {"id": chunk["id"]},
                )
                all_chunks.append(chunk)
        document_ids.append(str(doc["id"]))
        await repo.update(
            "knowledge_documents",
            doc["id"],
            {
                "checksum": checksum,
                "extraction_quality": {
                    "parser": "PYPDF_STRICT_V1",
                    "pages": len(pages),
                    "ocr": False,
                },
            },
        )
    from services.advisory.provider_policy import authorized

    embedding = pc["embedding"]
    await authorized(repo, [embedding["model"]])
    manifest = {
        "document_ids": sorted(document_ids),
        "processing_status": "SUCCEEDED",
        "processing_job_id": str(job_id),
        "managed_source_hash": frozen["managed_source_hash"],
        "managed_sources": frozen["managed_sources"],
        "chunks": [
            {"id": str(row["id"]), "hash": row["text_hash"]} for row in all_chunks
        ],
    }
    corpus = await repo.insert(
        "knowledge_corpus_versions",
        {
            "manifest": manifest,
            "manifest_hash": digest(manifest),
            "ingestion_config": pc,
            "embedding_config": embedding,
            "retrieval_config": retrieval_config(all_chunks),
            "pipeline_version": pc["pipeline_version"],
            "status": "DRAFT",
            "author_id": actor["id"],
        },
    )
    if not all_chunks or len(all_chunks) > 5000:
        fail(
            413,
            "CORPUS_CHUNK_LIMIT",
            "Use a public source set with 1 to 5000 text chunks.",
        )
    for chunk in all_chunks:
        vector = await embed(
            chunk["text"], model=embedding["model"], dimension=embedding["dimension"]
        )
        await repo.conn.execute(
            text(
                "INSERT INTO knowledge_embeddings(chunk_id,corpus_version,embedding_model,embedding_version,dimension,embedding) VALUES(:chunk,:corpus,:model,:version,:dimension,CAST(:embedding AS vector))"
            ),
            {
                "chunk": chunk["id"],
                "corpus": corpus["id"],
                "model": embedding["model"],
                "version": embedding["version"],
                "dimension": embedding["dimension"],
                "embedding": "[" + ",".join(map(str, vector)) + "]",
            },
        )
    await unchanged(repo, frozen)
    return str(corpus["id"])


async def activate(repo, frozen, corpus_id):
    await repo.conn.execute(
        text("LOCK TABLE institution_knowledge_sources IN SHARE MODE")
    )
    await unchanged(repo, frozen)
    corpus = await repo.get("knowledge_corpus_versions", corpus_id, lock=True)
    if (
        corpus["status"] != "DRAFT"
        or corpus["manifest"].get("managed_source_hash")
        != frozen["managed_source_hash"]
    ):
        fail(
            409,
            "CORPUS_SOURCE_CHANGED",
            "Only the exact successfully indexed PDFs can be activated.",
        )
    for row in frozen["documents"]:
        doc = await repo.get("knowledge_documents", row["id"], lock=True)
        if doc["status"] != "DRAFT" or doc["artifact_hash"] != row["artifact_hash"]:
            fail(
                409,
                "ARTIFACT_HASH_CHANGED",
                "The indexed PDF version changed before activation.",
            )
        await repo.update(
            "knowledge_documents",
            row["id"],
            {"status": "ACTIVE", "validated_at": datetime.now(timezone.utc)},
        )
    await repo.update(
        "knowledge_corpus_versions",
        corpus_id,
        {"status": "ACTIVE", "published_at": datetime.now(timezone.utc)},
    )
    return corpus_id
