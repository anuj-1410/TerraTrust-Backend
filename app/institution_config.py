"""Additive configuration. No network, database, or provider calls on import."""

from typing import ClassVar
from pydantic import BaseModel, Field, model_validator


RETIRED_ENV_KEYS = frozenset(
    {
        "INSTITUTION_MALWARE_SCANNER_HOST",
        "INSTITUTION_MALWARE_SCANNER_PORT",
        "ACTION_RANKING_CONFIG_VERSION",
        "PRIVATE_STORAGE_BUCKET",
        "REPORT_STORAGE_BUCKET",
        "CAMPUS_SAMPLE_QUALITY_POLICY",
        "SATELLITE_FEATURE_SCHEMA",
        "INSTITUTION_PROVIDER_POLICY_VERSION",
        "INSTITUTION_RETENTION_POLICY_VERSION",
        "RERANKER_MODEL",
        "AUDIT_DEMO_MODE",
        "ACTIVE_ACCOUNTING_METHOD_VERSION",
        "BIOMASS_MODEL_VERSION",
        "ACTIVE_CORPUS_VERSION",
        "INSTITUTION_RECEIPT_KMS_KEY_ID",
        "NISAR_ENABLED",
        "BIOMASS_MODEL_BACKEND",
        "CHUNK_OVERLAP_TOKENS",
        "CHUNK_SIZE_TOKENS",
        "DEPLOYMENT_ENV",
        "EMBEDDING_API_KEY",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_BATCH_SIZE",
        "EMBEDDING_PROVIDER",
        "INSTITUTION_ACCOUNTING_DEADLINE_SECONDS",
        "INSTITUTION_ADVICE_PER_HOUR",
        "INSTITUTION_ADVISORY_CONCURRENCY",
        "INSTITUTION_ADVISORY_DEADLINE_SECONDS",
        "INSTITUTION_API_READS_PER_MINUTE",
        "INSTITUTION_API_WRITES_PER_MINUTE",
        "INSTITUTION_BATCH_MAX_ITEMS",
        "INSTITUTION_COMMISSIONING_UID",
        "INSTITUTION_CSV_MAX_BYTES",
        "INSTITUTION_DEMO_DATABASE_URL",
        "INSTITUTION_DEMO_DEPLOYMENT_ID",
        "INSTITUTION_DOWNLOAD_MAX_BYTES",
        "INSTITUTION_EXPORT_DEADLINE_SECONDS",
        "INSTITUTION_INGEST_DEADLINE_SECONDS",
        "INSTITUTION_JOB_MAX_RETRIES",
        "INSTITUTION_JSON_MAX_BYTES",
        "INSTITUTION_OFFLINE_MAX_SECONDS",
        "INSTITUTION_RASTER_CONCURRENCY",
        "INSTITUTION_RASTER_DEADLINE_SECONDS",
        "INSTITUTION_SIGNED_DOWNLOAD_TTL_SECONDS",
        "INSTITUTION_SIGNED_UPLOAD_TTL_SECONDS",
        "INSTITUTION_UPLOAD_DEADLINE_SECONDS",
        "INSTITUTION_UPLOAD_MAX_BYTES",
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MAX_OUTPUT_TOKENS",
        "LLM_PROVIDER",
        "LLM_TIMEOUT_SECONDS",
        "RAG_LEXICAL_CANDIDATES",
        "RAG_RERANK_CANDIDATES",
        "RAG_RRF_K",
        "RAG_TOP_K",
        "RAG_VECTOR_CANDIDATES",
    }
)


class InstitutionSettings(BaseModel):
    INSTITUTION_SCHEMA_ENABLED: bool = False
    ENABLE_INSTITUTION_FLOW: bool = False
    ENABLE_INSTITUTION_ADVISOR: bool = False
    ENABLE_INSTITUTION_DEMO_ACCOUNTS: bool = False
    INSTITUTION_REPORT_FONT_PATH: str = ""
    INSTITUTION_REPORT_FONT_SHA256: str = ""
    INSTITUTION_PRIVACY_POLICY_URL: str = ""
    INSTITUTION_SUPPORT_URL: str = ""
    INSTITUTION_ACCOUNT_DELETION_URL: str = ""
    GEMINI_API_KEY: str = ""
    LLM_MODEL: str = ""
    EMBEDDING_MODEL: str = ""
    EMBEDDING_DIMENSION: int = Field(0, ge=0)

    # Protocol limits are reviewed code constants, not deployment inputs.
    PRIVATE_STORAGE_BUCKET: ClassVar[str] = "institution-evidence"
    REPORT_STORAGE_BUCKET: ClassVar[str] = "institution-reports"
    LLM_PROVIDER: ClassVar[str] = "GEMINI"
    LLM_TIMEOUT_SECONDS: ClassVar[int] = 60
    LLM_MAX_OUTPUT_TOKENS: ClassVar[int] = 2000
    EMBEDDING_PROVIDER: ClassVar[str] = "GEMINI"
    EMBEDDING_BATCH_SIZE: ClassVar[int] = 1
    ACTION_RANKING_CONFIG_VERSION: ClassVar[str] = "RULES_V1"
    RAG_VECTOR_CANDIDATES: ClassVar[int] = 20
    RAG_LEXICAL_CANDIDATES: ClassVar[int] = 20
    RAG_RERANK_CANDIDATES: ClassVar[int] = 30
    RAG_TOP_K: ClassVar[int] = 6
    RAG_RRF_K: ClassVar[int] = 60
    CHUNK_SIZE_TOKENS: ClassVar[int] = 800
    CHUNK_OVERLAP_TOKENS: ClassVar[int] = 120
    INSTITUTION_API_READS_PER_MINUTE: ClassVar[int] = 60
    INSTITUTION_API_WRITES_PER_MINUTE: ClassVar[int] = 30
    INSTITUTION_ADVICE_PER_HOUR: ClassVar[int] = 10
    INSTITUTION_UPLOAD_MAX_BYTES: ClassVar[int] = 10485760
    INSTITUTION_CSV_MAX_BYTES: ClassVar[int] = 5242880
    INSTITUTION_JSON_MAX_BYTES: ClassVar[int] = 1048576
    INSTITUTION_DOWNLOAD_MAX_BYTES: ClassVar[int] = 20971520
    INSTITUTION_BATCH_MAX_ITEMS: ClassVar[int] = 200
    INSTITUTION_SIGNED_UPLOAD_TTL_SECONDS: ClassVar[int] = 300
    INSTITUTION_SIGNED_DOWNLOAD_TTL_SECONDS: ClassVar[int] = 60
    INSTITUTION_OFFLINE_MAX_SECONDS: ClassVar[int] = 86400
    INSTITUTION_JOB_MAX_RETRIES: ClassVar[int] = 3
    INSTITUTION_RASTER_DEADLINE_SECONDS: ClassVar[int] = 21600
    INSTITUTION_ACCOUNTING_DEADLINE_SECONDS: ClassVar[int] = 600
    INSTITUTION_EXPORT_DEADLINE_SECONDS: ClassVar[int] = 600
    INSTITUTION_UPLOAD_DEADLINE_SECONDS: ClassVar[int] = 300
    INSTITUTION_ADVISORY_DEADLINE_SECONDS: ClassVar[int] = 90
    INSTITUTION_INGEST_DEADLINE_SECONDS: ClassVar[int] = 1800
    INSTITUTION_RASTER_CONCURRENCY: ClassVar[int] = 1
    INSTITUTION_ADVISORY_CONCURRENCY: ClassVar[int] = 1

    @property
    def DEPLOYMENT_ENV(self):
        return getattr(self, "ENVIRONMENT", "development")

    @property
    def INSTITUTION_DEMO_DEPLOYMENT_ID(self):
        # Stable resource identity shared by API/workers; no extra env value.
        import hashlib

        project = getattr(self, "FIREBASE_PROJECT_ID", "")
        storage = getattr(self, "SUPABASE_URL", "").rstrip("/")
        return (
            hashlib.sha256((project + "|" + storage).encode()).hexdigest()[:24]
            if project and storage
            else ""
        )

    @model_validator(mode="before")
    @classmethod
    def discard_retired_environment_knobs(cls, values):
        # Accept old dotenv templates without letting retired keys override code.
        if isinstance(values, dict):
            return {
                key: value
                for key, value in values.items()
                if key not in RETIRED_ENV_KEYS
            }
        return values

    @model_validator(mode="after")
    def institution_config_consistency(self):
        if self.ENABLE_INSTITUTION_ADVISOR and not self.ENABLE_INSTITUTION_FLOW:
            raise ValueError("Institution advisor requires institution flow.")
        if self.ENABLE_INSTITUTION_DEMO_ACCOUNTS and (
            self.DEPLOYMENT_ENV not in {"development", "test"}
            or getattr(self, "ENVIRONMENT", None) == "production"
        ):
            raise ValueError(
                "ENABLE_INSTITUTION_DEMO_ACCOUNTS is forbidden in production."
            )
        if self.CHUNK_OVERLAP_TOKENS >= self.CHUNK_SIZE_TOKENS:
            raise ValueError("Chunk overlap must be smaller than chunk size.")
        # Enabled configuration fails its own readiness check. Farmer startup is
        # unaffected; an unavailable institution provider never disables farming.
        return self

    def institution_missing_config(self, advisor=False):
        names = [
            "INSTITUTION_PRIVACY_POLICY_URL",
            "INSTITUTION_SUPPORT_URL",
            "INSTITUTION_ACCOUNT_DELETION_URL",
        ]
        if advisor:
            names += [
                "GEMINI_API_KEY",
                "LLM_MODEL",
                "EMBEDDING_MODEL",
                "EMBEDDING_DIMENSION",
            ]
        return [name for name in names if not getattr(self, name)]
