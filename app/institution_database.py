"""Lazy TLS-verified institution pool, independent of the farmer connection."""

from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.database_ssl import database_ssl_context
from app.institution_errors import fail

_engine = None


def _require_async_engine():
    global _engine
    if not settings.INSTITUTION_SCHEMA_ENABLED or not settings.DATABASE_URL:
        fail(503, "INSTITUTION_FLOW_DISABLED", "The institution schema connection is not commissioned.")
    if _engine is None:
        _engine = create_async_engine(settings.DATABASE_URL, echo=False, pool_pre_ping=True, pool_size=5, max_overflow=5, pool_timeout=10, connect_args={"ssl": database_ssl_context(settings.DATABASE_SSL_CA_FILE), "command_timeout": 60, "server_settings": {"search_path": "public,extensions"}})
    return _engine


async def close():
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
