"""Actual file checks, code-owned rules and fixed-resource configuration."""

import hashlib
import io
from copy import deepcopy

import pytest
from PIL import Image

from app.config import settings
from app.institution_config import InstitutionSettings
from app.institution_errors import InstitutionError
from app.institution_rules import retention_rules
from models.institution.common import digest
from scripts.institution.supabase_setup import storage_sql


def test_resource_names_are_fixed_and_legacy_environment_cannot_override_them():
    configured = InstitutionSettings.model_validate(
        {"PRIVATE_STORAGE_BUCKET": "other", "REPORT_STORAGE_BUCKET": "other"}
    )
    assert configured.PRIVATE_STORAGE_BUCKET == "institution-evidence"
    assert configured.REPORT_STORAGE_BUCKET == "institution-reports"
    assert "PRIVATE_STORAGE_BUCKET" not in type(configured).model_fields


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_real_upload_bytes_are_verified_and_promoted_without_an_external_service(
    monkeypatch, changed
):
    from services.shared import evidence_service

    image = io.BytesIO()
    Image.new("RGB", (3, 3), "green").save(image, format="PNG")
    data, writes = image.getvalue(), []

    async def read(bucket, key):
        return data

    async def write(bucket, key, content, mime):
        writes.append((key, content, mime))

    monkeypatch.setattr(evidence_service, "read_bytes", read)
    monkeypatch.setattr(evidence_service, "write_bytes", write)
    row = {
        "id": "actual-byte-fixture",
        "private_bucket": "institution-evidence",
        "object_key": "pending/source",
        "size_bytes": len(data),
        "expected_sha256": "0" * 64 if changed else hashlib.sha256(data).hexdigest(),
        "mime_type": "image/png",
        "file_name": "capture.png",
        "parent_type": "SURVEY",
    }
    if changed:
        with pytest.raises(InstitutionError) as error:
            await evidence_service.verify_and_promote(row)
        assert error.value.code == "FILE_HASH_MISMATCH" and not writes
    else:
        result = await evidence_service.verify_and_promote(row)
        assert result["state"] == "READY" and result["actual_size_bytes"] == len(data)
        assert writes == [(result["object_key"], data, "image/png")]


@pytest.mark.asyncio
async def test_gemini_rules_are_applied_without_database_policy_setup(monkeypatch):
    from services.advisory.provider_policy import authorized

    monkeypatch.setattr(settings, "GEMINI_API_KEY", "offline-key")
    monkeypatch.setattr(settings, "LLM_MODEL", "configured-generation")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "configured-embedding")

    class NoPolicyDatabase:
        async def get(self, *args, **kwargs):
            pytest.fail("Code-owned provider rules require no policy lookup.")

    value = await authorized(
        NoPolicyDatabase(), ["configured-generation", "configured-embedding"]
    )
    assert value["id"] == "TT_GEMINI_PUBLIC_FACTS_V1" and len(value["hash"]) == 64
    with pytest.raises(InstitutionError):
        await authorized(NoPolicyDatabase(), ["unconfigured-model"])


@pytest.mark.asyncio
async def test_deletion_rules_apply_without_manual_policy_record(monkeypatch):
    from services.institution.privacy_service import policy

    monkeypatch.setattr(
        settings, "INSTITUTION_PRIVACY_POLICY_URL", "https://example.invalid/privacy"
    )

    class NoPolicyDatabase:
        async def get(self, *args, **kwargs):
            pytest.fail("New deletion requests use application rules directly.")

    selected = await policy(NoPolicyDatabase())
    assert selected["artifact_hash"] == digest(selected["config"])
    assert (
        next(
            row
            for row in selected["config"]["categories"]
            if row["category"] == "UNBOUND_PERSONAL_UPLOADS"
        )["deletion_rule"]
        == "DELETE_ON_VERIFIED_ACCOUNT_DELETION"
    )
    assert all(
        row["duration_days"] is None
        for row in selected["config"]["categories"]
        if row["category"] == "ANNUAL_RESULT"
    )


@pytest.mark.asyncio
async def test_previous_accepted_deletion_rules_survive_later_configuration_changes(
    monkeypatch,
):
    from services.institution.privacy_service import policy

    old = retention_rules("https://old.invalid/privacy")
    monkeypatch.setattr(
        settings, "INSTITUTION_PRIVACY_POLICY_URL", "https://new.invalid/privacy"
    )

    class SavedRules:
        async def get(self, *args, **kwargs):
            return None

        async def one(self, *args, **kwargs):
            return {"rules": deepcopy(old)}

    assert await policy(SavedRules(), old["id"]) == old


def test_reranking_weights_come_from_actual_corpus_text():
    from services.advisory.ingestion_rules import retrieval_config
    from services.advisory.retrieval_service import rerank

    config = retrieval_config(
        [{"text": "solar electricity"}, {"text": "water electricity"}]
    )
    weights = config["reranker"]["idf"]
    assert weights["solar"] > weights["electricity"] > 0
    assert rerank("solar", "solar electricity", config["reranker"]) > rerank(
        "solar", "water", config["reranker"]
    )
    assert config == retrieval_config(
        [{"text": "solar electricity"}, {"text": "water electricity"}]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("rules_state", ["valid", "missing", "modified"])
async def test_real_pdf_ingestion_builds_a_corpus_without_deployment_policy_rows(
    monkeypatch,
    rules_state,
):
    from uuid import uuid4

    from reportlab.pdfgen.canvas import Canvas

    from app.institution_rules import RETRIEVAL_RULES_VERSION
    from services.advisory import ingestion_service

    stream = io.BytesIO()
    canvas = Canvas(stream)
    canvas.drawString(30, 750, "Solar electricity and water measurement guidance.")
    canvas.save()
    data = stream.getvalue()
    checksum = hashlib.sha256(data).hexdigest()
    document_id, evidence_id = str(uuid4()), str(uuid4())
    tables = {
        "knowledge_documents": [
            {
                "id": document_id,
                "status": "DRAFT",
                "version": 1,
                "artifact_hash": "a" * 64,
                "source_evidence_id": evidence_id,
                "config": {"source_type": "PDF", "source_byte_hash": checksum},
            }
        ],
        "institution_audit_evidence": [
            {
                "id": evidence_id,
                "parent_type": "KNOWLEDGE",
                "parent_id": document_id,
                "institution_id": None,
                "state": "READY",
                "bytes_deleted_at": None,
                "byte_hash": checksum,
                "mime_type": "application/pdf",
                "private_bucket": "institution-evidence",
                "object_key": "verified/unit-source",
            }
        ],
    }

    class Connection:
        async def execute(self, *args, **kwargs):
            return None

    class CorpusRepo:
        conn = Connection()

        async def one(self, sql, params=None, **kwargs):
            # This case starts without an active base corpus.
            assert "knowledge_corpus_versions" in sql
            return None

        async def get(self, table, rid, **kwargs):
            assert table != "institution_deployment_policies"
            return next(row for row in tables[table] if row["id"] == rid)

        async def find(self, table, filters):
            return [
                row
                for row in tables.get(table, [])
                if all(row.get(key) == value for key, value in filters.items())
            ]

        async def insert(self, table, values):
            row = {"id": str(uuid4()), **values}
            tables.setdefault(table, []).append(row)
            return row

        async def update(self, table, rid, values):
            row = await self.get(table, rid)
            row.update(values)
            return row

        async def rows(self, sql, values):
            return [
                row
                for row in tables.get("knowledge_chunks", [])
                if row["document_id"] in values["ids"]
            ]

    async def read(bucket, key):
        return data

    async def embed(text, **kwargs):
        assert text and kwargs == {"model": "configured-embedding", "dimension": 3}
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(settings, "GEMINI_API_KEY", "offline-key")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "configured-embedding")
    monkeypatch.setattr(settings, "EMBEDDING_DIMENSION", 3)
    monkeypatch.setattr(ingestion_service, "read_bytes", read)
    monkeypatch.setattr(ingestion_service, "embed", embed)
    from services.advisory.ingestion_rules import configuration

    frozen_rules = await configuration(CorpusRepo())
    # Later deployments cannot change the already accepted source set.
    if rules_state == "missing":
        frozen_rules = None
    elif rules_state == "modified":
        frozen_rules["config"]["chunk_size"] += 1
    frozen = {
        "document_id": document_id,
        "expected_artifact_hash": "a" * 64,
        "job_id": str(uuid4()),
        "ingestion_rules": frozen_rules,
    }
    if rules_state != "valid":
        with pytest.raises(InstitutionError) as error:
            await ingestion_service.ingest(CorpusRepo(), frozen, {"id": str(uuid4())})
        assert error.value.code == "INGESTION_CONFIG_CHANGED"
        assert not tables.get("knowledge_corpus_versions")
        return
    corpus_id = await ingestion_service.ingest(
        CorpusRepo(),
        frozen,
        {"id": str(uuid4())},
    )
    corpus = next(
        row for row in tables["knowledge_corpus_versions"] if row["id"] == corpus_id
    )
    assert corpus["pipeline_version"] == RETRIEVAL_RULES_VERSION
    assert corpus["manifest"]["processing_status"] == "SUCCEEDED"
    assert corpus["retrieval_config"]["reranker"]["idf"]["solar"] > 0


def test_storage_setup_creates_only_fixed_private_buckets_without_overwriting():
    parser = pytest.importorskip("pglast")
    sql = storage_sql()
    parser.parse_sql(sql)
    parser.parse_plpgsql(sql)
    assert "ON CONFLICT (id) DO NOTHING" in sql and "20971520" in sql
    assert "institution-evidence" in sql and "institution-reports" in sql
    assert "UPDATE" not in sql and "DELETE" not in sql
    assert "AS RESTRICTIVE FOR ALL TO anon,authenticated" in sql
    assert "USING (bucket_id NOT IN ('institution-evidence','institution-reports'))" in sql
    assert "WITH CHECK (bucket_id NOT IN ('institution-evidence','institution-reports'))" in sql
    assert "DROP POLICY" not in sql
