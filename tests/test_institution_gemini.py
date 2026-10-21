import httpx
import pytest
from app.config import settings
from app.institution_errors import InstitutionError
from services.advisory import gemini_provider as provider


def configured(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "unit-test-placeholder")
    monkeypatch.setattr(settings, "LLM_MODEL", "commissioned-test-model")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "gemini-embedding-001")
    monkeypatch.setattr(settings, "EMBEDDING_DIMENSION", 3)


@pytest.mark.asyncio
async def test_free_quota_is_explicit_and_never_calls_a_paid_fallback(monkeypatch):
    configured(monkeypatch)
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(429, json={"error":{"message":"quota"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(InstitutionError) as error:
            await provider.generate("redacted facts", {"type":"object"}, client=client)
    assert error.value.code == "ADVISOR_QUOTA_EXCEEDED"
    assert error.value.retryable and len(calls) == 1
    assert calls[0].url.host == "generativelanguage.googleapis.com"
    assert calls[0].headers["x-goog-api-key"] == "unit-test-placeholder"


@pytest.mark.asyncio
async def test_unknown_provider_acceptance_is_not_retryable(monkeypatch):
    configured(monkeypatch)
    def handle(request):
        raise httpx.ReadTimeout("timeout", request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(InstitutionError) as error:
            await provider.generate("safe facts", {}, client=client)
    assert error.value.code == "EXTERNAL_OUTCOME_UNKNOWN"
    assert error.value.retryable is False


@pytest.mark.asyncio
async def test_embedding_dimension_and_normalization_are_verified(monkeypatch):
    configured(monkeypatch)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"embedding":{"values":[3,4,0]}}))) as client:
        assert await provider.embed("public source", client=client) == [0.6,0.8,0.0]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"embedding":{"values":[0,1]}}))) as client:
        with pytest.raises(InstitutionError):
            await provider.embed("public source", client=client)
