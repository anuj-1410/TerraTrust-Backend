"""Immutable fact context, bounded generation, independent publication."""
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
from uuid import uuid4

from app.config import settings
from app.institution_access import require_flow
from app.institution_errors import InstitutionError, fail
from models.institution.common import digest, normalize
from models.institution.contracts import MODELS, parse
from services.institution import annual_service
from services.institution.job_service import accepted, enqueue
from services.institution.factor_service import ranking_rules
from services.advisory import gemini_provider
from services.advisory.retrieval_service import retrieve, retrieval_limits

PROMPT_VERSION = "TT_FACTS_AND_PUBLIC_PASSAGES_V1"
NUMBERS = re.compile(r"(?<![\w])[-+]?\d+(?:\.\d+)?")


def redact(text):
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[redacted email]", text)
    text = re.sub(r"(?<!\w)\+?\d[\d ()-]{7,}\d", "[redacted identifier]", text)
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "[redacted identifier]", text, flags=re.I)
    text = re.sub(r"https?://\S+", "[redacted URL]", text)
    text = re.sub(r"[-+]?\d{1,3}\.\d{3,}\s*,\s*[-+]?\d{1,2}\.\d{3,}","[redacted coordinates]",text)
    return text[:2000]


def redact_values(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_values(item) for item in value]
    return value


def provider_question(question, topics=()):
    """Only server-owned intent words cross the free-tier privacy boundary.

    Redaction alone cannot recognize names or volunteered confidential details.
    Keep the original question private and embed/generate from this bounded
    vocabulary instead. Unknown questions receive generic, cited guidance.
    """
    vocabulary = ("solar", "rainwater", "biogas", "composting", "electricity", "transport", "fuel", "waste", "water", "emissions", "carbon", "trees", "stock", "removal", "release", "balance", "efficiency", "measurement", "accounting")
    words = set(re.findall(r"[a-z]+", question.casefold()))
    intents = [word for word in vocabulary if word in words]
    return "Explain applicable public guidance for institution carbon assessment: " + (", ".join(intents) if intents else "accounting and evidence requirements") + ". Use only supplied aggregate facts and cited public passages."


def facts(result):
    output = []
    for key in ("actual_total", "inventory_balance", "tree_stock", "removal", "release", "quantified_actual_subtotal", "quantified_avoided_total"):
        output.append({"fact_id": "F_" + key.upper(), "value": result.get(key), "unit": "tCO2" if key in {"tree_stock", "removal", "release"} else "tCO2e", "snapshot_hash": result["result_hash"]})
    for key, value in sorted(result.get("by_category", {}).items()):
        output.append({"fact_id": "F_CATEGORY_" + key, "value": value, "unit": "tCO2e", "snapshot_hash": result["result_hash"]})
    return output


async def resolve_context(ctx, body):
    await ctx.access("institutions", body["institution_id"], "W")
    aid = body["audit_id"]
    if aid is None:
        rows = await ctx.repo.rows("SELECT id FROM institution_audits WHERE institution_id=:iid AND business_status='FINALIZED' ORDER BY period_end DESC,created_at DESC LIMIT 1", {"iid": ctx.iid})
        if not rows:
            fail(409, "NO_FINALIZED_CONTEXT", "Finalize an assessment before using the default advisor context.")
        aid = str(rows[0]["id"])
    audit = await ctx.repo.get("institution_audits", aid)
    if str(audit["institution_id"]) != ctx.iid:
        fail(404, "RESOURCE_NOT_FOUND", "Context is unavailable.")
    sid = body["snapshot_id"] or audit["reviewed_snapshot_id"] or audit["preview_snapshot_id"]
    if sid is None:
        fail(409, "NO_CALCULATED_CONTEXT", "Calculate this revision before asking for advice.")
    snapshot = await ctx.repo.get("institution_calculation_snapshots", sid)
    if str(snapshot["audit_id"]) != str(audit["id"]):
        fail(404, "RESOURCE_NOT_FOUND", "Context is unavailable.")
    ctx.audit, ctx.snapshot = audit, snapshot


async def ask(ctx, body):
    require_flow(advisor=True)
    from services.advisory.provider_policy import authorized
    provider_policy = await authorized(ctx.repo, [settings.LLM_MODEL, settings.EMBEDDING_MODEL])
    from services.advisory.corpus_service import current
    corpus = await current(ctx.repo)
    if corpus["status"] != "ACTIVE":
        fail(503, "CORPUS_NOT_READY", "No independently activated corpus is available.")
    embedding = corpus["embedding_config"]
    if embedding.get("provider") != "GEMINI" or embedding.get("model") != settings.EMBEDDING_MODEL or embedding.get("dimension") != settings.EMBEDDING_DIMENSION:
        fail(503,"CORPUS_CONFIGURATION_MISMATCH","The configured embedding model/dimension must match the activated corpus.")
    audit, snapshot = ctx.audit, ctx.snapshot
    institution = await ctx.repo.get("institutions", ctx.iid)
    context = {"institution_id": ctx.iid, "audit_id": str(audit["id"]), "revision": audit["revision"], "snapshot_id": str(snapshot["id"]), "status": audit["business_status"], "snapshot_hash": snapshot["result_hash"]}
    versions = {"corpus": str(corpus["id"]), "corpus_hash": corpus["manifest_hash"], "embedding": corpus["embedding_config"], "reranker": corpus["retrieval_config"].get("reranker", {}).get("version"), "llm_provider": "GEMINI", "llm_model": settings.LLM_MODEL, "prompt": PROMPT_VERSION, "ranking": settings.ACTION_RANKING_CONFIG_VERSION, "retrieval_limits": retrieval_limits(), "classifier": "TT_BOUNDED_FACT_ROUTER_V1", "provider_policy": provider_policy}
    constraints = body["constraints"]
    # Provider payload has aggregate facts and redacted volunteered constraints,
    # never the relational context IDs or profile/evidence/geometry snapshots.
    provider_constraints = {k: v for k, v in constraints.items() if k in {"time_horizon_months", "guidance_as_of"}}
    if constraints.get("budget"):
        provider_constraints["budget"] = {"value":constraints["budget"]["value"],"currency":constraints["budget"]["currency"],"source":"User-declared, unverified"}
    for key in ("preferences","exclusions"):
        provider_constraints[key] = [value for value in constraints.get(key,[]) if value in {"SOLAR","RAINWATER","BIOGAS","COMPOSTING"}]
    provider_constraints = redact_values(provider_constraints)
    provider_constraints.setdefault("guidance_as_of",date.today().isoformat())
    address = institution.get("profile", {}).get("address", {})
    country, state = address.get("country"), address.get("state")
    geography_codes = [value for value in (state, "IN" if str(country).casefold() in {"india", "in"} else country, "GLOBAL") if value]
    ranking_config = snapshot["frozen_input"]["method"]["config"]["ranking_config"]
    ranking_rules(ranking_config)
    frozen = {"context": context, "facts": facts(snapshot["result"]), "question": redact(body["question"]), "provider_question": provider_question(body["question"]), "constraints": provider_constraints, "topics": body.get("topics", []), "geography": state, "geography_codes": geography_codes, "versions": versions, "corpus": normalize(corpus), "ranking_config": ranking_config}
    frozen["input_hash"] = digest(frozen)
    unresolved = await ctx.repo.rows("SELECT j.id FROM processing_jobs j WHERE j.job_type='advisory' AND j.requested_by=:uid AND j.institution_id=:iid AND j.input_hash=:hash AND (j.error->>'code'='EXTERNAL_OUTCOME_UNKNOWN' OR EXISTS(SELECT 1 FROM institution_provider_operations p WHERE p.job_id=j.id AND p.status IN ('STARTED','UNKNOWN'))) LIMIT 1",{"uid":ctx.actor["id"],"iid":ctx.iid,"hash":frozen["input_hash"]})
    if unresolved:
        fail(409,"EXTERNAL_OUTCOME_UNKNOWN","An equivalent advisory operation has an unresolved provider outcome; use its existing job reference.")
    qid, jid = str(uuid4()), str(uuid4())
    query = await ctx.repo.insert("advisory_queries", {"id": qid, "institution_id": ctx.iid, "user_id": ctx.actor["id"], "audit_id": audit["id"], "snapshot_id": snapshot["id"], "question": body["question"], "constraints": constraints, "context_snapshot": context, "context_hash": snapshot["result_hash"], "versions": versions, "job_id": jid, "status": "QUEUED", "publication_state": "PRIVATE", "timings": {}, "usage": {}, "warnings": []})
    job = await enqueue(ctx, "advisory", query, frozen, asset_type="ADVISORY_QUERY", job_id=jid)
    ctx.context_id = str(snapshot["id"])
    return accepted(job)


def abstention(known, reason):
    return {"summary": reason, "facts": known, "document_claims": [], "recommendations": [], "assumptions": [], "missing_data": ["Applicable supported guidance"], "warnings": [], "supported": False}


def validate_answer(answer, known, sources, action_catalog, ranking_config=None):
    answer = parse("AdvisoryAnswer", answer)
    by_fact = {f["fact_id"]: f for f in known}
    by_source = {s["citation_id"]: s for s in sources}
    if any(by_fact.get(f["fact_id"]) != f for f in answer["facts"]):
        raise ValueError("A generated fact differs from the immutable context.")
    for claim in answer["document_claims"]:
        if not claim["citation_ids"] or not set(claim["citation_ids"]) <= by_source.keys():
            raise ValueError("Unsupported citation.")
        available = set(NUMBERS.findall(" ".join(by_source[c]["excerpt"] for c in claim["citation_ids"])))
        if set(NUMBERS.findall(claim["text"])) - available:
            raise ValueError("Unsupported document numeric claim.")
    for recommendation in answer["recommendations"]:
        catalog = action_catalog.get(recommendation["action_id"])
        if not catalog or not recommendation["citations"] or not set(recommendation["citations"]) <= by_source.keys():
            raise ValueError("Unknown or unsupported action.")
        if NUMBERS.search(recommendation["title"] + " " + recommendation["rationale"]):
            raise ValueError("Generated actions cannot assert numeric impacts.")
        if recommendation["overlap_refs"] or recommendation["scenario_id"]:
            raise ValueError("Only the backend can attach private overlap/scenario identities.")
        recommendation["scenario_capability"] = catalog.get("scenario_capability")
        recommendation["priority_class"] = "DATA_REQUIRED" if recommendation["missing_data"] else "SITE_CHECK_REQUIRED"
        # A language model cannot establish that site prerequisites are met.
        recommendation["prerequisites"] = list(dict.fromkeys(catalog["prerequisites"] + recommendation["prerequisites"]))
    allowed_summary_numbers = set(NUMBERS.findall(" ".join(str(f["value"]) for f in known if f["value"] is not None)))
    if set(NUMBERS.findall(answer["summary"])) - allowed_summary_numbers:
        raise ValueError("The answer summary introduces an unsupported quantity.")
    order = ranking_rules(ranking_config) if ranking_config is not None else {"DATA_REQUIRED": 0, "SUPPORTED_ACTION": 1, "SITE_CHECK_REQUIRED": 2}
    total = next((Decimal(f["value"]) for f in known if f["fact_id"] == "F_ACTUAL_TOTAL" and f["value"] is not None), None)
    def category_share(recommendation):
        category = action_catalog[recommendation["action_id"]].get("category")
        value = by_fact.get("F_CATEGORY_" + str(category), {}).get("value")
        with localcontext() as context:
            context.prec = 50
            return Decimal(value) / total if total is not None and total > 0 and value is not None else None
    def ranking(recommendation):
        share = category_share(recommendation)
        return order[recommendation["priority_class"]], share is None, -share if share is not None else Decimal(0), recommendation["action_id"]
    answer["recommendations"].sort(key=ranking)
    for rank, recommendation in enumerate(answer["recommendations"], 1):
        recommendation["priority"] = rank
    if answer["supported"] and not answer["facts"] and not answer["document_claims"]:
        raise ValueError("No substantive support.")
    return answer


async def run(repo, frozen, query):
    known, question = frozen["facts"], frozen["question"]
    direct_keys = {"actual total": "F_ACTUAL_TOTAL", "inventory balance": "F_INVENTORY_BALANCE", "tree stock": "F_TREE_STOCK"}
    direct = next((key for phrase, key in direct_keys.items() if phrase in question.casefold()), None)
    usage, sources, response_id = {}, [], None
    if direct:
        fact = next(f for f in known if f["fact_id"] == direct)
        answer = {"summary": "The selected snapshot records " + str(fact["value"]) + " " + fact["unit"] + "." if fact["value"] is not None else "This value is unavailable in the selected snapshot.", "facts": [fact], "document_claims": [], "recommendations": [], "assumptions": [], "missing_data": [] if fact["value"] is not None else [direct], "warnings": [], "supported": fact["value"] is not None}
    else:
        safe_question = frozen.get("provider_question") or provider_question(question)
        retrieved = await retrieve(repo, frozen["corpus"], safe_question, as_of=frozen["constraints"].get("guidance_as_of", date.today().isoformat()), geography=frozen.get("geography_codes", ["GLOBAL"]), topics=frozen["topics"], limits=frozen["versions"].get("retrieval_limits"))
        sources = [row["source"] for row in retrieved]
        for rank, row in enumerate(retrieved, 1):
            await repo.insert("advisory_retrievals", {"institution_id": query["institution_id"], "query_id": query["id"], "chunk_id": row["source"]["chunk_id"], "corpus_version": frozen["corpus"]["id"], "chunk_version": row["source"]["document_version"], "rank": rank, "fusion_score": row["fusion"], "reranker_score": row["reranker"], "filter_decisions": {"visibility": "PUBLIC_APPROVED", "applicable": True}})
        if not sources:
            answer = abstention(known, "No applicable approved guidance supports an answer to this question.")
        else:
            payload = {"question": safe_question, "facts": known, "constraints": frozen["constraints"], "sources": sources}
            try:
                answer, usage, response_id = await gemini_provider.generate(json.dumps(payload, ensure_ascii=False), MODELS["AdvisoryAnswer"].model_json_schema())
                answer = validate_answer(answer, known, sources, frozen["corpus"]["retrieval_config"].get("action_catalog", {}), frozen["ranking_config"])
            except InstitutionError as exc:
                if exc.code != "ADVISOR_INVALID_RESPONSE":
                    raise
                answer = abstention(known, "Generation did not return a complete validated answer.")
            except (ValueError, TypeError, KeyError):
                answer = abstention(known, "Generated guidance did not pass fact and citation validation.")
    stored = {"answer": answer, "sources": sources}
    return {"answer": stored, "answer_hash": digest(stored), "status": "READY" if answer["supported"] else "PARTIAL", "usage": {**usage, "provider_response_id": response_id}, "cost": None}


def resource(row):
    saved = row.get("answer") or {}
    answer = saved.get("answer")
    return {"query_id": str(row["id"]), "job_id": str(row["job_id"]), "status": row["status"], "publication_state": row["publication_state"], "context": row["context_snapshot"], "answer": answer, "recommendations": answer["recommendations"] if answer else [], "sources": saved.get("sources", []), "missing_data": answer["missing_data"] if answer else [], "warnings": row["warnings"], "versions": row["versions"], "input_revision": row["input_revision"], "answer_hash": row["answer_hash"]}
