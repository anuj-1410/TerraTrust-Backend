"""Application-owned Gemini model and data-handling requirements."""

from app.config import settings
from app.institution_errors import fail
from app.institution_rules import provider_rules


async def authorized(repo, models):
    configured = {settings.LLM_MODEL, settings.EMBEDDING_MODEL} - {""}
    if (
        not settings.GEMINI_API_KEY
        or not models
        or any(not model for model in models)
        or not set(models) <= configured
    ):
        fail(
            503,
            "GEMINI_NOT_CONFIGURED",
            "Use the configured Gemini generation and embedding models.",
        )
    return provider_rules(models)
