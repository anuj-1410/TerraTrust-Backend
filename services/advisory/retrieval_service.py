"""Version-pinned applicability, PostgreSQL hybrid retrieval and local reranking."""
import math
import re
from collections import Counter
from datetime import date

from app.institution_errors import fail
from models.institution.common import digest, numeric
from decimal import Decimal
from services.advisory.gemini_provider import embed
from app.config import settings


def retrieval_limits():
    return {"vector": settings.RAG_VECTOR_CANDIDATES, "lexical": settings.RAG_LEXICAL_CANDIDATES, "rerank": settings.RAG_RERANK_CANDIDATES, "top_k": settings.RAG_TOP_K, "rrf_k": settings.RAG_RRF_K}


def reciprocal_rank(lists, k=60):
    scores = {}
    for ordered in lists:
        for rank, key in enumerate(ordered, 1):
            scores[key] = scores.get(key, 0) + 1 / (k + rank)
    return sorted(scores, key=lambda key: (-scores[key], key)), scores


def applicable(document, as_of, geography, topics):
    if document["status"] != "ACTIVE" or document["config"]["visibility"] != "PUBLIC_APPROVED" or not document["license"]:
        return False
    if document.get("valid_from") and str(document["valid_from"]) > as_of:
        return False
    if document.get("valid_to") and str(document["valid_to"]) < as_of:
        return False
    codes = document["geography_codes"]
    if codes and not set(codes) & set(geography):
        return False
    return not topics or bool(set(topics) & set(document["config"]["topic_codes"]))


def rerank(query, text, config):
    # This explicit baseline is a reproducible local model, never a made-up
    # cross-encoder score. New corpus weights are computed from its actual text.
    if config.get("engine") != "TFIDF_COSINE_V1" or not (config.get("version") or config.get("evaluation_evidence_id")):
        fail(503, "RERANKER_NOT_CONFIGURED", "A versioned corpus-derived local reranker is required.")
    weights = config.get("idf", {})
    def vector(value):
        terms = Counter(re.findall(r"\w+", value.casefold(), re.UNICODE))
        return {key: count * float(weights[key]) for key, count in terms.items() if key in weights}
    a, b = vector(query), vector(text)
    denominator = math.sqrt(sum(x*x for x in a.values()) * sum(x*x for x in b.values()))
    return sum(value * b.get(key, 0) for key, value in a.items()) / denominator if denominator else 0.0


async def retrieve(repo, corpus, question, *, as_of, geography, topics, limits=None):
    if corpus["status"] != "ACTIVE":
        fail(503, "CORPUS_NOT_READY", "The selected corpus is not active.")
    config, embedding = corpus["retrieval_config"], corpus["embedding_config"]
    documents = []
    for did in corpus["manifest"].get("document_ids", []):
        row = await repo.get("knowledge_documents", did)
        if applicable(row, as_of, geography, topics):
            documents.append(row)
    if not documents:
        return []
    by_document = {str(row["id"]): row for row in documents}
    ids = list(by_document)
    vector = await embed(question, query=True, model=embedding["model"], dimension=embedding["dimension"])
    limits = limits or retrieval_limits()
    params = {"ids": ids, "corpus": corpus["id"], "vector": "[" + ",".join(map(str, vector)) + "]", "query": question, "vector_limit": limits["vector"], "lexical_limit": limits["lexical"]}
    semantic = await repo.rows("SELECT c.*,1-(e.embedding <=> CAST(:vector AS vector)) AS score FROM knowledge_chunks c JOIN knowledge_embeddings e ON e.chunk_id=c.id WHERE e.corpus_version=:corpus AND c.document_id=ANY(CAST(:ids AS uuid[])) ORDER BY e.embedding <=> CAST(:vector AS vector),c.id LIMIT :vector_limit", params)
    lexical = await repo.rows("SELECT c.*,ts_rank_cd(c.lexical_search_vector,websearch_to_tsquery('simple',:query)) AS score FROM knowledge_chunks c WHERE c.document_id=ANY(CAST(:ids AS uuid[])) AND c.lexical_search_vector @@ websearch_to_tsquery('simple',:query) ORDER BY score DESC,c.id LIMIT :lexical_limit", params)
    ordered, fused = reciprocal_rank([[str(r["id"]) for r in rows] for rows in (semantic, lexical)], k=limits["rrf_k"])
    by_chunk = {str(row["id"]): row for row in semantic + lexical}
    ranked = [(key, rerank(question, by_chunk[key]["text"], config["reranker"])) for key in ordered[:limits["rerank"]]]
    ranked.sort(key=lambda pair: (-pair[1], -fused[pair[0]], pair[0]))
    output, seen = [], set()
    for key, score in ranked:
        chunk = by_chunk[key]
        if chunk["text_hash"] in seen:
            continue
        seen.add(chunk["text_hash"])
        document = by_document[str(chunk["document_id"])]
        maximum = config.get("max_excerpt_characters")
        if type(maximum) is not int or not 1 <= maximum <= 2000:
            fail(503, "SOURCE_LICENSE_NOT_CONFIGURED", "Commission the licensed excerpt limit.")
        source = {"citation_id": "C_" + digest({"chunk": key, "version": chunk["document_version"]})[:24], "document_id": str(document["id"]), "chunk_id": key, "document_version": chunk["document_version"], "title": document["title"], "publisher": document["publisher"], "source_url": document["source_url"], "page": str(chunk["page_start"]) if chunk["page_start"] else None, "section": chunk["section"], "excerpt": chunk["text"][:maximum], "checksum": document["checksum"], "applicability": {"valid_from": str(document["valid_from"]) if document["valid_from"] else None, "valid_to": str(document["valid_to"]) if document["valid_to"] else None, "geography_codes": document["geography_codes"], "notes": document["applicability_notes"]}}
        output.append({"source": source, "fusion": numeric(Decimal(str(fused[key]))), "reranker": numeric(Decimal(str(score)))})
        if len(output) == limits["top_k"]:
            break
    return output
