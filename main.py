"""TerraTrust-AR FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from starlette.exceptions import HTTPException as StarletteHTTPException

load_dotenv()

from app.config import settings
from app.database import verify_database_setup
from app.firebase_auth import get_firebase_app
from app.gee import ensure_gee_initialized
from routers import auth, land, audit, credits

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("terratrust")

ALLOWED_CORS_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
ALLOWED_CORS_HEADERS = [
    "Authorization",
    "Content-Type",
    "Accept",
    "Origin",
    "X-Requested-With",
]


def _get_cors_origins() -> list[str]:
    """Return explicit CORS origins compatible with credentialed requests."""
    configured_origins = [
        origin.strip()
        for origin in (settings.WEB_CORS_ORIGINS or "").split(",")
        if origin.strip()
    ]

    if configured_origins:
        return configured_origins

    if settings.ENVIRONMENT == "development":
        return [
            "http://localhost:8081",
            "http://127.0.0.1:8081",
            "http://localhost:19006",
            "http://127.0.0.1:19006",
        ]

    return []


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialise critical SDKs during application lifespan startup."""
    startup_errors: list[str] = []

    try:
        await verify_database_setup()
        logger.info("Direct PostgreSQL/PostGIS connection verified.")
    except Exception as exc:
        logger.exception("Failed to verify direct PostgreSQL/PostGIS connection.")
        startup_errors.append(f"Database/PostGIS startup verification failed: {exc}")

    try:
        get_firebase_app()
        logger.info("Firebase Admin initialised.")
    except Exception as exc:
        logger.exception("Failed to initialise Firebase Admin.")
        startup_errors.append(f"Firebase Admin initialisation failed: {exc}")

    try:
        ensure_gee_initialized()
        logger.info("Google Earth Engine initialised.")
    except Exception as exc:
        logger.exception("Failed to initialise Google Earth Engine.")
        startup_errors.append(f"Google Earth Engine initialisation failed: {exc}")

    if startup_errors:
        raise RuntimeError("; ".join(startup_errors))

    yield

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="TerraTrust-AR API",
    description="Autonomous carbon credit verification for Indian smallholder farmers.",
    version="3.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS middleware
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=_get_cors_origins(),
    allow_credentials=True,
    allow_methods=ALLOWED_CORS_METHODS,
    allow_headers=ALLOWED_CORS_HEADERS,
)

# ---------------------------------------------------------------------------
# Include routers
# ---------------------------------------------------------------------------
app.include_router(auth.router, prefix="/api/v1/auth", tags=["Auth"])
app.include_router(land.router, prefix="/api/v1/land", tags=["Land"])
app.include_router(audit.router, prefix="/api/v1/audit", tags=["Audit"])
app.include_router(credits.router, prefix="/api/v1/credits", tags=["Credits"])

# -- Demo account support (development only) ---------------------------
import os as _os
if _os.getenv("ENABLE_DEMO_ACCOUNTS", "false").lower() == "true":
    from demo.middleware import DemoResetMiddleware
    from demo.router import router as _demo_router

    app.add_middleware(DemoResetMiddleware)
    app.include_router(_demo_router, prefix="/demo")
# ----------------------------------------------------------------------


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(_request: Request, exc: StarletteHTTPException):
    """Return documented error payloads while preserving structured details."""
    content = exc.detail if isinstance(exc.detail, dict) else {"error": exc.detail}
    return JSONResponse(
        status_code=exc.status_code,
        content=content,
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    _request: Request,
    exc: RequestValidationError,
):
    """Collapse FastAPI validation errors into the documented API error envelope."""
    messages: list[str] = []
    for error in exc.errors():
        location = " -> ".join(str(item) for item in error.get("loc", []))
        message = error.get("msg", "Invalid value.")
        messages.append(f"{location}: {message}" if location else message)

    return JSONResponse(
        status_code=422,
        content={
            "error": "Invalid request payload.",
            "details": messages,
        },
    )


@app.middleware("http")
async def maintenance_mode_middleware(request: Request, call_next):
    """Return the documented 503 payload when maintenance mode is active."""
    exempt_paths = {
        "/health",
        "/api/v1/status",
        "/docs",
        "/redoc",
        "/openapi.json",
    }

    if settings.MAINTENANCE_MODE and request.url.path not in exempt_paths:
        return JSONResponse(
            status_code=503,
            content={
                "maintenance": True,
                "message": settings.MAINTENANCE_MESSAGE or "Scheduled maintenance in progress",
            },
        )

    return await call_next(request)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health", tags=["Health"])
async def health_check():
    """Return basic service health status."""
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def root_status():
    """Return a lightweight root payload for platform probes and browsers."""
    return {
        "service": "TerraTrust-AR API",
        "status": "ok",
        "docs_url": "/docs",
        "health_url": "/health",
        "api_status_url": "/api/v1/status",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/v1/status", tags=["Health"])
async def application_status():
    """Return the documented farmer-facing maintenance status payload."""
    maintenance = bool(settings.MAINTENANCE_MODE)
    return {
        "maintenance": maintenance,
        "message": settings.MAINTENANCE_MESSAGE if maintenance else None,
    }


