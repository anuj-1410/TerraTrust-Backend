"""Select the latest compatible activated corpus; no deployment UUID pin."""

from app.config import settings
from app.institution_errors import fail


async def current(repo, *, required=True):
    row = await repo.one(
        "SELECT * FROM knowledge_corpus_versions WHERE status='ACTIVE' AND embedding_config->>'provider'='GEMINI' AND embedding_config->>'model'=:model AND embedding_config->>'dimension'=:dimension ORDER BY published_at DESC NULLS LAST,created_at DESC,id DESC LIMIT 1",
        {
            "model": settings.EMBEDDING_MODEL,
            "dimension": str(settings.EMBEDDING_DIMENSION),
        },
        required=False,
    )
    if row:
        managed = row["manifest"].get("managed_sources")
        if managed is not None:
            from services.advisory.managed_sources import source_snapshot, sources

            if source_snapshot(await sources(repo)) != managed:
                if required:
                    fail(
                        503,
                        "CORPUS_NOT_READY",
                        "Public PDF sources changed; run scripts.institution.sync_knowledge.",
                    )
                return None
        return row
    if required:
        fail(
            503,
            "CORPUS_NOT_READY",
            "Index the configured public PDFs with scripts.institution.sync_knowledge.",
        )
    return None
