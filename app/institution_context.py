"""One transaction per institution request; durable receipts precede stale checks."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy import text

from app.config import settings
from app.institution_access import identity, user_for_claims, tenant_access
from app.institution_errors import fail, InstitutionError
from app.redis_utils import redis_from_url, secure_redis_url
from models.institution.common import digest, normalize, canonical
from models.institution.contracts import parse, MODELS
from repositories.institution.base import Repository

router = APIRouter()
openapi_bearer = HTTPBearer(auto_error=False, scheme_name="InstitutionFirebaseBearer", description="Firebase phone ID token. The server verifies identity and revocation.")


class Context:
    def __init__(self, request, repo, actor, claims):
        self.request, self.repo, self.actor, self.claims = request, repo, actor, claims
        self.request_id = str(uuid4())
        request.state.request_id = self.request_id
        self.iid, self.member, self.parent = None, None, None
        self.revision, self.context_id, self.warnings = None, None, []
        self.secret = None
        self.generation = None

    async def access(self, table, resource_id, mode="R", *, own_pending=False, lock=False):
        lock = lock or getattr(self, "lock_mode", False) or self.request.method != "GET"
        row = await self.repo.get(table, resource_id)
        iid = row["id"] if table == "institutions" else row["institution_id"]
        _, self.member = await tenant_access(self.repo, self.actor, iid, mode, own_pending=own_pending, lock=lock)
        if lock and table != "institutions":
            row = await self.repo.get(table, resource_id, lock=True)
        self.iid, self.parent = str(iid), row
        if not settings.ENABLE_INSTITUTION_FLOW and mode == "R" and self.member:
            self.member = {**self.member, "permission": "VIEWER"}
        if not settings.ENABLE_INSTITUTION_FLOW and self.request.method == "GET" and mode in {"W", "O"} and "/jobs/" not in self.request.url.path:
            fail(503, "INSTITUTION_FLOW_DISABLED", "Only released history and own privacy status are available.")
        await self.check_generation()
        return row

    async def check_generation(self):
        if not settings.ENABLE_INSTITUTION_DEMO_ACCOUNTS:
            return
        from demo.institution.sessions import current_state
        row = await current_state(self.actor["firebase_uid"], self.actor["phone_number"])
        if row:
            supplied = self.request.headers.get("X-TerraTrust-Demo-Generation")
            stale_login = row["reset_policy"] != "PERSISTENT" and self.claims.get("auth_time") != row["auth_time"]
            if stale_login or supplied is not None and supplied != str(row["fixture_generation"]):
                fail(409, "DEMO_SESSION_RESET", "Refresh the current demo session before continuing.")
            self.generation = row["fixture_generation"]
            if self.iid and not await self.repo.one("SELECT id FROM institutions WHERE id=:iid AND created_by=:uid", {"iid": self.iid, "uid": self.actor["id"]}, required=False):
                fail(404, "RESOURCE_NOT_FOUND", "Resource is unavailable.")

    def if_match(self):
        value = self.request.headers.get("If-Match")
        if value is None:
            return None
        if not re.fullmatch(r'"[1-9][0-9]*"', value):
            fail(400, "INVALID_PRECONDITION", "If-Match must be a quoted positive integer.")
        return int(value[1:-1])

    def match(self, row):
        revision = self.if_match()
        if revision is None:
            fail(428, "PRECONDITION_REQUIRED", "The original If-Match revision is required.")
        if revision != row["input_revision"]:
            fail(409, "AUDIT_REVISION_CONFLICT", "The saved draft changed. Compare your edits with the server.", current_revision=row["input_revision"])

    async def rate_limit(self):
        write = self.request.method != "GET"
        advice = self.request.url.path.endswith("/advisory/ask")
        limit = settings.INSTITUTION_ADVICE_PER_HOUR if advice else settings.INSTITUTION_API_WRITES_PER_MINUTE if write else settings.INSTITUTION_API_READS_PER_MINUTE
        window = 3600 if advice else 60
        key = f"tt:institution:rate:{self.actor['id']}:{self.iid or 'own'}:{'advice' if advice else 'write' if write else 'read'}"
        script = "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],ARGV[1]) end; return n"
        try:
            def consume():
                with redis_from_url(secure_redis_url(settings.REDIS_URL), socket_timeout=2, socket_connect_timeout=2) as client:
                    return client.eval(script, 1, key, window)
            count = await asyncio.to_thread(consume)
        except Exception:
            fail(503, "LIMITER_UNAVAILABLE", "Request controls are temporarily unavailable.", retryable=True)
        if count > limit:
            fail(429, "RATE_LIMITED", "Request limit exceeded.", retryable=True)

    async def receipt(self, body):
        key = self.request.headers.get("Idempotency-Key", "")
        if not key or len(key) > 128 or not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            fail(400, "IDEMPOTENCY_KEY_REQUIRED", "Supply a stable Idempotency-Key for this operation.")
        path = self.request.url.path
        if "//" in path or "%" in path or "\\" in path:
            fail(400, "INVALID_ROUTE", "Ambiguous request path.")
        query = {k: self.request.query_params.getlist(k) for k in sorted(self.request.query_params.keys())}
        request_hash = digest({"method": self.request.method, "route": path, "query": query, "body": body, "if_match": self.if_match(), "fixture_generation": self.generation})
        scope = {"user_id": str(self.actor["id"]), "institution_id": self.iid, "http_method": self.request.method, "normalized_route": path, "idempotency_key": key, "fixture_generation": self.generation}
        await self.repo.conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"), {"scope": digest(scope)})
        rows = await self.repo.find("idempotency_records", scope, lock=True, limit=1)
        self.receipt_scope, self.request_hash = scope, request_hash
        if not rows:
            return None
        receipt = rows[0]
        if path == "/api/v1/institutions" and receipt["accepted_resource_id"]:
            await tenant_access(self.repo,self.actor,receipt["accepted_resource_id"],"O",own_pending=True)
        if receipt["request_hash"] != request_hash:
            fail(409, "IDEMPOTENCY_CONFLICT", "This key was already used for different request content.")
        if receipt["receipt_state"] == "TOMBSTONE" or receipt["expires_at"] <= datetime.now(timezone.utc):
            fail(409, "IDEMPOTENCY_RECEIPT_EXPIRED", "The original operation committed; refresh its resource or explicitly reissue the invitation.", details={"accepted_resource_id": str(receipt["accepted_resource_id"]) if receipt["accepted_resource_id"] else None, "original_http_status": receipt["http_status"], "mutation_committed": True, "accepted_revision": receipt["accepted_revision"], "recovery_kind": receipt["recovery_kind"]})
        result = receipt["response"]
        if receipt["secret_ciphertext"]:
            from services.shared.receipt_crypto_service import decrypt_receipt
            if receipt["secret_expires_at"] <= datetime.now(timezone.utc):
                fail(409, "IDEMPOTENCY_RECEIPT_EXPIRED", "The invitation receipt expired; explicitly reissue the code.")
            result["data"]["invitation_token"] = await decrypt_receipt(receipt, scope, request_hash)
        result["request_id"] = self.request_id
        return receipt["http_status"], result

    async def save_receipt(self, status, result):
        data = result["data"]
        resource_id = data.get("resource_id") or data.get("id") or data.get("deleted_id") or data.get("batch_id") if isinstance(data, dict) else None
        values = {**self.receipt_scope, "request_hash": self.request_hash, "intended_revision": self.if_match(), "http_status": status, "accepted_resource_id": resource_id, "accepted_revision": self.revision, "response": result, "receipt_state": "FULL", "recovery_kind": "DELETE_ACK" if self.request.method == "DELETE" else "RESOURCE_GET", "expires_at": datetime.now(timezone.utc) + timedelta(hours=72)}
        if self.secret:
            values.update(self.secret)
            values["recovery_kind"] = "SECRET_REISSUE"
        await self.repo.insert("idempotency_records", values)

    def success(self, data):
        output = {"data": normalize(data), "request_id": self.request_id, "warnings": self.warnings, "revision": self.revision, "context_id": self.context_id}
        if self.generation:
            output["fixture_generation"] = self.generation
        return output

    async def page(self, table, filters, serializer=None):
        from repositories.institution.base import columns
        if not set(filters) <= columns(table):
            raise ValueError("Unknown page filter.")
        predicates = [f"{key} IS NULL" if value is None else f"{key}=:{key}" for key, value in filters.items()]
        where = " AND ".join(predicates) or "TRUE"
        summary_sql = f"SELECT count(*) AS total,md5(COALESCE(string_agg(id::text||updated_at::text,',' ORDER BY id),'')) AS snapshot FROM {table} WHERE {where}"
        summary = await self.repo.one(summary_sql, filters)
        scope = digest({"actor":str(self.actor["id"]),"iid":self.iid,"permission":self.member["permission"] if self.member else None,"table":table,"filters":filters,"snapshot":summary["snapshot"]})
        try:
            limit = int(self.request.query_params.get("limit", "25"))
            if not 1 <= limit <= 100:
                raise ValueError()
        except ValueError:
            fail(422, "INVALID_PAGE", "Page limit must be an integer from 1 through 100.")
        offset = self.decode_cursor(scope)
        rows = await self.repo.rows(f"SELECT {self.repo.selection(table)} FROM {table} WHERE {where} ORDER BY created_at DESC,id DESC LIMIT :_limit OFFSET :_offset", {**filters,"_limit":limit,"_offset":offset})
        import inspect
        output = []
        for row in rows:
            value = serializer(row) if serializer else row
            output.append(await value if inspect.isawaitable(value) else value)
        if (await self.repo.one(summary_sql, filters))["snapshot"] != summary["snapshot"]:
            fail(409, "PAGE_SNAPSHOT_CHANGED", "The list changed. Restart from its first page.")
        return {"items":output,"total":summary["total"],"next_cursor":self.encode_cursor(scope,offset+len(rows)) if offset+len(rows)<summary["total"] else None}

    def decode_cursor(self, scope):
        cursor = self.request.query_params.get("cursor")
        if not cursor:
            return 0
        try:
            state = json.loads(base64.urlsafe_b64decode(cursor))
            signature = hmac.new(settings.SUPABASE_SERVICE_KEY.encode(),canonical(state["payload"]),hashlib.sha256).hexdigest()
            offset = state["payload"]["offset"]
            if not hmac.compare_digest(signature,state["signature"]) or state["payload"]["scope"] != scope or type(offset) is not int or offset<0:
                raise ValueError()
            return offset
        except Exception:
            fail(409,"PAGE_SNAPSHOT_CHANGED","The list changed. Restart from its first page.")

    def encode_cursor(self, scope, offset):
        payload = {"scope":scope,"offset":offset}
        signature = hmac.new(settings.SUPABASE_SERVICE_KEY.encode(),canonical(payload),hashlib.sha256).hexdigest()
        return base64.urlsafe_b64encode(canonical({"payload":payload,"signature":signature})).decode()

    async def page_rows(self, rows, serializer=None, *, scope_data=None):
        try:
            limit = int(self.request.query_params.get("limit", "25"))
        except ValueError:
            fail(422, "INVALID_PAGE", "Page limit must be an integer.")
        if limit < 1 or limit > 100:
            fail(422, "INVALID_PAGE", "Page limit must be between 1 and 100.")
        snapshot = digest(rows)
        scope = digest({"uid": str(self.actor["id"]), "iid": self.iid, "permission": self.member["permission"] if self.member else None, "query": {k:v for k,v in self.request.query_params.items() if k not in {"cursor", "limit"}}, "scope": scope_data, "snapshot": snapshot})
        cursor, offset = self.request.query_params.get("cursor"), 0
        if cursor:
            try:
                state = json.loads(base64.urlsafe_b64decode(cursor))
                # Cursor carries no private data; use the server's existing secret
                # as HMAC key, not an unprotected client-controlled offset.
                sig = hmac.new(settings.SUPABASE_SERVICE_KEY.encode(), canonical(state["payload"]), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(sig, state["signature"]) or state["payload"]["scope"] != scope:
                    raise ValueError()
                offset = state["payload"]["offset"]
                if type(offset) is not int or offset < 0:
                    raise ValueError()
            except Exception:
                fail(409, "PAGE_SNAPSHOT_CHANGED", "The list changed. Restart from the first page.")
        selected = rows[offset:offset + limit]
        import inspect
        items = []
        for row in selected:
            value = serializer(row) if serializer else row
            items.append(await value if inspect.isawaitable(value) else value)
        next_cursor = None
        if offset + limit < len(rows):
            payload = {"scope": scope, "offset": offset + limit}
            signature = hmac.new(settings.SUPABASE_SERVICE_KEY.encode(), canonical(payload), hashlib.sha256).hexdigest()
            next_cursor = base64.urlsafe_b64encode(canonical({"payload": payload, "signature": signature})).decode()
        return {"items": items, "next_cursor": next_cursor, "total": len(rows)}


def commissioning_write(ctx):
    path = ctx.request.url.path
    registries = ("emission-factors", "carbon-methodologies", "models", "knowledge-documents")
    if any(path == "/api/v1/admin/" + name or path.startswith("/api/v1/admin/" + name + "/") for name in registries):
        return True  # The route's prepare hook requires a global operator grant.
    if path.startswith("/api/v1/evidence/uploads"):
        from services.institution.evidence_service import GLOBALS
        parent_type = getattr(ctx, "evidence", {}).get("parent_type") or (ctx.body or {}).get("parent_type")
        return parent_type in GLOBALS
    return False


def endpoint(method, path, *, model=None, status=200, prepare=None, query=(), mutations=True):
    """Register a typed handler; commit before returning acceptance to the client."""
    def decorate(handler):
        async def execute(request: Request):
            if not settings.INSTITUTION_SCHEMA_ENABLED:
                fail(503, "INSTITUTION_FLOW_DISABLED", "Institution schema is not commissioned.")
            body = None
            from models.institution.common import uuid_text
            for key, value in request.path_params.items():
                if key == "category":
                    continue
                try:
                    uuid_text(value)
                except (ValueError, TypeError, AttributeError):
                    fail(422, "INVALID_RESOURCE_ID", "Path identifiers must be canonical UUIDs.")
            if method in {"POST", "PATCH", "PUT", "DELETE"}:
                chunks, size = [], 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > settings.INSTITUTION_JSON_MAX_BYTES:
                        fail(413, "REQUEST_TOO_LARGE", "Request exceeds the JSON body limit.")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                if raw:
                    try:
                        body = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")), object_pairs_hook=unique_object)
                    except (ValueError, UnicodeError):
                        fail(400, "MALFORMED_JSON", "Expected valid JSON.")
                    if not isinstance(body, dict):
                        fail(422, "INVALID_PAYLOAD", "Expected a JSON object.")
                    if isinstance(body.get("items"), list) and len(body["items"]) > settings.INSTITUTION_BATCH_MAX_ITEMS:
                        fail(422, "BATCH_LIMIT_EXCEEDED", "This request exceeds the configured batch size.")
                    depth_limit = 32 if request.url.path.startswith("/api/v1/admin/") and any(registry in request.url.path for registry in ("/carbon-methodologies","/models","/emission-factors","/knowledge-documents")) else 8
                    if json_depth(body) > depth_limit:
                        fail(422, "INVALID_PAYLOAD", "JSON nesting exceeds this endpoint's supported depth.")
                    if type(body.get("schema_version", 1)) is not int or body.get("schema_version", 1) != 1:
                        fail(422, "UNSUPPORTED_SCHEMA", "Only schema_version=1 is supported.")
                    supplied = dict(body) if model and "schema_version" in MODELS[model].model_fields else {k: v for k, v in body.items() if k != "schema_version"}
                    if model:
                        try:
                            validation_body = dict(supplied)
                            if "schema_version" in MODELS[model].model_fields:
                                validation_body.setdefault("schema_version", 1)
                            parse(model, validation_body)
                        except (ValidationError, ValueError) as exc:
                            errors = exc.errors(include_input=False, include_context=False) if isinstance(exc, ValidationError) else [{"loc": [], "msg": str(exc)}]
                            fail(422, "INVALID_PAYLOAD", "Correct the request fields.", fields=[{"path": "/" + "/".join(map(str, e["loc"])), "code": "INVALID_VALUE", "message": e["msg"]} for e in errors])
                    body_for_handler = supplied
                else:
                    body_for_handler = None
                    if model:
                        fail(422, "INVALID_PAYLOAD", "A request object is required.")
            else:
                body_for_handler = None
            allowed = {"limit", "cursor", *query}
            if set(request.query_params) - allowed:
                fail(422, "UNKNOWN_QUERY", "Unknown query parameter.")
            from models.institution.common import date_text
            for key in request.query_params:
                values = request.query_params.getlist(key)
                if len(values)>1 and key not in {"audit_ids","campus_ids"}:
                    fail(422,"DUPLICATE_QUERY","Only audit_ids and campus_ids may repeat.")
                try:
                    for value in values:
                        if key.endswith(("_id","_ids")) or key in {"method_version","model_version"}:
                            uuid_text(value)
                        if key in {"period_start","period_end","activity_date"}:
                            date_text(value)
                        if key == "since_input_revision" and (not value.isdecimal() or int(value)<0):
                            raise ValueError()
                except (ValueError,TypeError,AttributeError):
                    fail(422,"INVALID_QUERY","Correct the query identifier, date or counter.")
            claims = await identity(request.headers.get("Authorization"))
            from app.institution_database import _require_async_engine
            try:
                async with _require_async_engine().begin() as conn:
                    repo = Repository(conn)
                    # Identity first, tenant second. Privacy execution takes an
                    # exclusive identity lock before inventorying all tenants,
                    # so no personal mutation can escape the approved plan.
                    # Accept/submit can also update onboarding. Acquire the
                    # exclusive lock immediately, avoiding concurrent upgrades
                    # from shared locks on the same identity.
                    identity_lock = "UPDATE" if method != "GET" else None
                    actor = await user_for_claims(repo, claims, identity_lock)
                    if actor.get("account_domain") != "INSTITUTION" and not (request.url.path.startswith("/api/v1/admin/") or request.url.path.endswith(("/review", "/finalize")) or "/evidence" in request.url.path or "/jobs/" in request.url.path or "/artifacts/" in request.url.path or request.url.path.endswith(("/emission-factors", "/carbon-methodologies"))):
                        fail(403, "PERMISSION_DENIED", "Operator grants do not confer mobile institution membership.")
                    ctx = Context(request, repo, actor, claims)
                    ctx.body = body_for_handler
                    await ctx.check_generation()
                    if prepare:
                        await prepare(ctx)
                    if not settings.ENABLE_INSTITUTION_FLOW and method != "GET" and not any(part in request.url.path for part in ("/institution-account/", "/admin/deletion-requests/")) and not request.url.path.endswith("/withdraw") and not commissioning_write(ctx):
                        fail(503, "INSTITUTION_FLOW_DISABLED", "New institution work is disabled.")
                    await ctx.rate_limit()
                    if method != "GET" and mutations:
                        replay = await ctx.receipt(body)
                        if replay:
                            replay_headers = {"Cache-Control": "private, no-store"}
                            if replay[1].get("revision"):
                                replay_headers["ETag"] = f'"{replay[1]["revision"]}"'
                            if replay[1].get("fixture_generation"):
                                replay_headers["X-TerraTrust-Demo-Generation"] = str(replay[1]["fixture_generation"])
                            return JSONResponse(status_code=replay[0], content=replay[1], headers=replay_headers)
                        ctx.lock_mode = True
                        if prepare:
                            await prepare(ctx)
                    data = await handler(ctx, body_for_handler)
                    if method != "GET":
                        await user_for_claims(repo,claims)
                    if method != "GET" and ctx.member and ctx.iid:
                        await tenant_access(repo, actor, ctx.iid, "R", own_pending=True)
                    await ctx.check_generation()
                    if ctx.revision is None and isinstance(data, dict) and type(data.get("input_revision")) is int:
                        ctx.revision = data["input_revision"]
                    if isinstance(data, dict) and data.get("snapshot_id"):
                        ctx.context_id = str(data["snapshot_id"])
                    result = ctx.success(data)
                    response_status = getattr(ctx, "status_code", status)
                    if method != "GET" and mutations:
                        await ctx.save_receipt(response_status, result)
                    if getattr(ctx, "response_secret", None):
                        result["data"]["invitation_token"] = ctx.response_secret
                headers = {"Cache-Control":"private, no-store"}
                if ctx.revision:
                    headers["ETag"] = f'"{ctx.revision}"'
                if ctx.generation:
                    headers["X-TerraTrust-Demo-Generation"] = str(ctx.generation)
                return JSONResponse(status_code=response_status, content=result, headers=headers)
            except InstitutionError:
                raise
            except IntegrityError:
                fail(409, "CONSTRAINT_CONFLICT", "This operation conflicts with existing records.")
            except SQLAlchemyError:
                fail(503, "DATABASE_UNAVAILABLE", "Institution storage is temporarily unavailable.", retryable=True)
        # Dynamic Request-based routes still declare their full transport shape
        # so generated clients can send UUID paths, filters and preconditions.
        parameters = [{"name": name, "in": "path", "required": True, "schema": {"type": "string", **({"format": "uuid"} if name != "category" else {})}} for name in re.findall(r"\{([^}]+)\}", path)]
        for name in ("limit", "cursor", *query):
            schema = {"type": "string"}
            if name in {"audit_ids", "campus_ids"}:
                schema = {"type": "array", "items": {"type": "string", "format": "uuid"}}
            elif name.endswith(("_id", "_version")) and name not in {"schema_version"}:
                schema["format"] = "uuid"
            elif name in {"limit", "since_input_revision", "revision"}:
                schema = {"type": "integer", "minimum": 0 if name == "since_input_revision" else 1}
            elif name in {"period_start", "period_end", "activity_date"}:
                schema["format"] = "date"
            parameters.append({"name": name, "in": "query", "required": False, "schema": schema, **({"style": "form", "explode": True} if name in {"audit_ids", "campus_ids"} else {})})
        parameters.append({"name": "X-TerraTrust-Demo-Generation", "in": "header", "required": False, "description": "Optional stale-context check for institution demo clients; normal authentication also identifies the current sign-in.", "schema": {"type": "integer", "minimum": 1}})
        if method != "GET" and mutations:
            parameters.extend([{"name": "Idempotency-Key", "in": "header", "required": True, "schema": {"type": "string"}}, {"name": "If-Match", "in": "header", "required": False, "description": "Original quoted input counter, required when editing an existing versioned resource.", "schema": {"type": "string", "pattern": '^"[1-9][0-9]*"$'}}])
        extra = {"parameters": parameters}
        if model:
            schema = MODELS[model].model_json_schema()
            schema = inline_schema(schema, schema.get("$defs", {}))
            extra["requestBody"] = {"required": True, "content": {"application/json": {"schema": schema}}}
        elif method != "GET":
            extra["requestBody"] = {"required": False, "content": {"application/json": {"schema": {"type": "object"}}}}
        router.add_api_route(path, execute, methods=[method], status_code=status, name=method.lower()+"_"+re.sub(r"[^a-zA-Z0-9]+", "_", path), tags=["Institution"], openapi_extra=extra, dependencies=[Depends(openapi_bearer)])
        return handler
    return decorate


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key.")
        result[key] = value
    return result


def json_depth(value):
    children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else []
    return 1 + max((json_depth(child) for child in children), default=0)


def inline_schema(value, definitions):
    if isinstance(value, list):
        return [inline_schema(child, definitions) for child in value]
    if isinstance(value, dict):
        if "$ref" in value and value["$ref"].startswith("#/$defs/"):
            return inline_schema(definitions[value["$ref"].split("/")[-1]], definitions)
        return {key: inline_schema(child, definitions) for key, child in value.items() if key != "$defs"}
    return value
