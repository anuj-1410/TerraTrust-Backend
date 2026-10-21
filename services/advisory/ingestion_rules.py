"""Deterministic public-corpus rules; weights come from actual source chunks."""

import math
import re
from collections import Counter
from copy import deepcopy

from app.config import settings
from app.institution_errors import fail
from app.institution_rules import (
    ACTION_CATALOG,
    PUBLIC_SOURCE_HOSTS,
    RETRIEVAL_RULES_ID,
    RETRIEVAL_RULES_VERSION,
)
from models.institution.common import digest


async def configuration(repo):
    if not settings.EMBEDDING_MODEL or not 1 <= settings.EMBEDDING_DIMENSION <= 3072:
        fail(
            503,
            "INGESTION_NOT_CONFIGURED",
            "Configure the Gemini embedding model and supported output dimension.",
        )
    documents = []
    from services.advisory.corpus_service import current
    prior = await current(repo, required=False)
    if prior:
        documents = prior["manifest"].get("document_ids", [])
    config = {
        "tokenizer": "UTF8_BYTES_V1",
        "pipeline_version": RETRIEVAL_RULES_VERSION,
        "chunk_size": settings.CHUNK_SIZE_TOKENS,
        "overlap": settings.CHUNK_OVERLAP_TOKENS,
        "base_document_ids": sorted(set(map(str, documents))),
        "allowed_source_hosts": sorted(PUBLIC_SOURCE_HOSTS),
        "embedding": {
            "provider": "GEMINI",
            "model": settings.EMBEDDING_MODEL,
            "dimension": settings.EMBEDDING_DIMENSION,
            "version": settings.EMBEDDING_MODEL
            + ":"
            + str(settings.EMBEDDING_DIMENSION),
        },
    }
    return {"id": RETRIEVAL_RULES_ID, "artifact_hash": digest(config), "config": config}


def retrieval_config(chunks):
    counts = Counter()
    for chunk in chunks:
        counts.update(set(re.findall(r"\w+", chunk["text"].casefold(), re.UNICODE)))
    idf = {
        word: math.log((len(chunks) + 1) / (count + 1)) + 1
        for word, count in sorted(counts.items())
    }
    return {
        "reranker": {
            "engine": "TFIDF_COSINE_V1",
            "version": "TFIDF_COSINE_V1:" + digest(idf)[:16],
            "idf": idf,
        },
        "max_excerpt_characters": 1000,
        "action_catalog": deepcopy(ACTION_CATALOG),
    }
