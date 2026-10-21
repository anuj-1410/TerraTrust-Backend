"""Real PDF parsing and multi-source indexing through the managed DB path."""

import io
from copy import deepcopy
from uuid import uuid4

import pytest
from reportlab.pdfgen.canvas import Canvas

from app.config import settings
from app.institution_errors import InstitutionError
from services.advisory import managed_sources, corpus_service
from test_institution_shared_biomass_and_settings import MemoryRepo


def public_source(key):
    return {
        "id": str(uuid4()),
        "input_revision": 1,
        "logical_key": key,
        "title": key,
        "publisher": "Synthetic PDF test",
        "object_key": "knowledge/" + key + ".pdf",
        "license": "Test-created source",
        "source_url": None,
        "geography_codes": ["TEST"],
        "topic_codes": ["SOLAR"],
        "language": "en",
        "valid_from": None,
        "valid_to": None,
        "enabled": True,
        "public_licensed": True,
        "data_origin": "LIVE",
    }


@pytest.fixture
def providers(monkeypatch):
    files = {}
    for key in ("first", "second"):
        stream = io.BytesIO()
        canvas = Canvas(stream)
        canvas.drawString(
            30, 750, key + " solar electricity guidance for test PDF indexing."
        )
        canvas.save()
        files["knowledge/" + key + ".pdf"] = stream.getvalue()
    calls = []

    async def read(bucket, key):
        assert bucket == "institution-evidence"
        return files[key]

    async def write(bucket, key, data, mime):
        assert mime == "application/pdf" and key not in files
        files[key] = data

    async def grant(repo, actor, capability):
        assert capability == "KNOWLEDGE_REVIEW"

    async def embed(text, **kwargs):
        assert text and kwargs == {"model": "test-embedding", "dimension": 3}
        calls.append(text)
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(managed_sources, "read_bytes", read)
    monkeypatch.setattr(managed_sources, "write_bytes", write)
    monkeypatch.setattr(managed_sources, "operator_access", grant)
    monkeypatch.setattr(managed_sources, "embed", embed)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "offline-test-key")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "test-embedding")
    monkeypatch.setattr(settings, "EMBEDDING_DIMENSION", 3)
    return files, calls


@pytest.mark.asyncio
async def test_multiple_pdfs_index_once_and_activate_only_after_success(providers):
    repo, actor = MemoryRepo(), {"id": str(uuid4())}
    repo.tables["institution_knowledge_sources"] = [
        public_source("first"),
        public_source("second"),
    ]
    result = await managed_sources.prepare(repo, actor)
    assert result["status"] == "QUEUED"
    job = repo.tables["processing_jobs"][0]
    frozen = deepcopy(job["frozen_input"])
    assert len(frozen["documents"]) == 2 and len(repo.tables["job_outbox"]) == 1
    corpus_id = await managed_sources.ingest(repo, frozen, actor, job["id"])
    corpus = await repo.get("knowledge_corpus_versions", corpus_id)
    assert corpus["status"] == "DRAFT"
    assert await corpus_service.current(repo, required=False) is None
    assert len(corpus["manifest"]["document_ids"]) == 2
    assert len(providers[1]) == len(repo.tables["knowledge_chunks"]) == 2
    assert all(row["status"] == "DRAFT" for row in repo.tables["knowledge_documents"])
    await managed_sources.activate(repo, frozen, corpus_id)
    active = await corpus_service.current(repo)
    assert str(active["id"]) == corpus_id
    assert active["manifest"]["managed_source_hash"] == frozen["managed_source_hash"]
    assert all(row["status"] == "ACTIVE" for row in repo.tables["knowledge_documents"])
    assert (await managed_sources.prepare(repo, actor))["status"] == "UNCHANGED"


@pytest.mark.asyncio
async def test_source_edits_prevent_activation_and_stale_advice(providers):
    repo, actor = MemoryRepo(), {"id": str(uuid4())}
    source = public_source("first")
    repo.tables["institution_knowledge_sources"] = [source]
    await managed_sources.prepare(repo, actor)
    job = repo.tables["processing_jobs"][0]
    frozen = job["frozen_input"]
    corpus_id = await managed_sources.ingest(repo, frozen, actor, job["id"])
    source["enabled"] = False
    with pytest.raises(InstitutionError) as error:
        await managed_sources.activate(repo, frozen, corpus_id)
    assert error.value.code == "KNOWLEDGE_SOURCES_CHANGED"
    assert (await repo.get("knowledge_corpus_versions", corpus_id))["status"] == "DRAFT"
    source["enabled"] = True
    await managed_sources.activate(repo, frozen, corpus_id)
    source["input_revision"] += 1
    with pytest.raises(InstitutionError) as error:
        await corpus_service.current(repo)
    assert error.value.code == "CORPUS_NOT_READY"


@pytest.mark.asyncio
async def test_changed_pdf_without_table_revision_gets_new_registry_version(providers):
    repo, actor = MemoryRepo(), {"id": str(uuid4())}
    source = public_source("first")
    repo.tables["institution_knowledge_sources"] = [source]
    await managed_sources.prepare(repo, actor)
    first_job = repo.tables["processing_jobs"][0]
    first_id = await managed_sources.ingest(
        repo, first_job["frozen_input"], actor, first_job["id"]
    )
    await managed_sources.activate(repo, first_job["frozen_input"], first_id)
    first_document = deepcopy(repo.tables["knowledge_documents"][0])
    stream, canvas = io.BytesIO(), None
    canvas = Canvas(stream)
    canvas.drawString(30, 750, "Changed public guidance PDF for the same object path.")
    canvas.save()
    providers[0][source["object_key"]] = stream.getvalue()
    await managed_sources.prepare(repo, actor)
    assert source["input_revision"] == 1
    assert [row["version"] for row in repo.tables["knowledge_documents"]] == [1, 2]
    assert repo.tables["knowledge_documents"][0] == first_document
    second_job = repo.tables["processing_jobs"][1]
    second_id = await managed_sources.ingest(
        repo, second_job["frozen_input"], actor, second_job["id"]
    )
    await managed_sources.activate(repo, second_job["frozen_input"], second_id)
    assert str((await corpus_service.current(repo))["id"]) == second_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "problem", ["private", "invalid_pdf", "bytes_changed", "no_text"]
)
async def test_unsafe_or_changed_source_bytes_are_rejected(providers, problem):
    repo, actor = MemoryRepo(), {"id": str(uuid4())}
    source = public_source("first")
    repo.tables["institution_knowledge_sources"] = [source]
    files = providers[0]
    if problem == "private":
        source["public_licensed"] = False
    elif problem == "invalid_pdf":
        files[source["object_key"]] = b"not a PDF"
    elif problem == "no_text":
        from pypdf import PdfWriter

        output, writer = io.BytesIO(), PdfWriter()
        writer.add_blank_page(100, 100)
        writer.write(output)
        files[source["object_key"]] = output.getvalue()
    if problem in {"private", "invalid_pdf"}:
        with pytest.raises(InstitutionError):
            await managed_sources.prepare(repo, actor)
        assert not repo.tables.get("processing_jobs")
        return
    await managed_sources.prepare(repo, actor)
    job = repo.tables["processing_jobs"][0]
    if problem == "bytes_changed":
        evidence = repo.tables["institution_audit_evidence"][0]
        files[evidence["object_key"]] += b"tampered"
    with pytest.raises(InstitutionError) as error:
        await managed_sources.ingest(repo, job["frozen_input"], actor, job["id"])
    assert error.value.code in {"SOURCE_HASH_CHANGED", "DOCUMENT_NEEDS_OCR"}
    assert not providers[1]


@pytest.mark.asyncio
async def test_corpus_selection_matches_embedding_without_environment_uuid(monkeypatch):
    from datetime import datetime, timezone

    repo = MemoryRepo()
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "selected")
    monkeypatch.setattr(settings, "EMBEDDING_DIMENSION", 3)
    for name in ("selected", "other"):
        await repo.insert(
            "knowledge_corpus_versions",
            {
                "status": "ACTIVE",
                "published_at": datetime.now(timezone.utc),
                "manifest": {},
                "embedding_config": {
                    "provider": "GEMINI",
                    "model": name,
                    "dimension": 3,
                },
            },
        )
    assert (await corpus_service.current(repo))["embedding_config"][
        "model"
    ] == "selected"
