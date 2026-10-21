"""Safe errors scoped to new institution endpoints."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from uuid import uuid4


class InstitutionError(Exception):
    def __init__(self, status: int, code: str, message: str, *, fields=None,
                 retryable=False, current_revision=None, details=None):
        self.status, self.code, self.message = status, code, message
        self.fields = fields or []
        self.retryable, self.current_revision, self.details = retryable, current_revision, details
        super().__init__(message)

    def payload(self, request_id=None):
        return {"code": self.code, "message": self.message, "field_errors": self.fields,
                "retryable": self.retryable, "request_id": request_id or str(uuid4()),
                "current_revision": self.current_revision, "details": self.details}


def fail(status, code, message, **kwargs):
    raise InstitutionError(status, code, message, **kwargs)


async def error_handler(request: Request, exc: InstitutionError):
    return JSONResponse(status_code=exc.status, content=exc.payload(getattr(request.state, "request_id", None)),
                        headers={"Cache-Control": "no-store", **({"Retry-After": "60"} if exc.status == 429 else {})})
