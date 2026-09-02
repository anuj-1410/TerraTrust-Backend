"""Celery application — background task infrastructure."""

from celery import Celery

from app.config import settings
from app.redis_utils import secure_redis_url

_CELERY_BROKER_URL = secure_redis_url(settings.REDIS_URL)

celery_app = Celery(
    "terratrust",
    broker=_CELERY_BROKER_URL,
    include=[
        "tasks.fusion_task",
        "tasks.minting_task",
    ],
)

# ---------------------------------------------------------------------------
# Celery configuration
# ---------------------------------------------------------------------------
celery_app.conf.update(
    task_time_limit=300,            # hard timeout: 5 minutes
    task_soft_time_limit=270,       # soft timeout: 4.5 minutes
    task_ignore_result=True,        # audit state is stored in Supabase, not Redis
    task_store_errors_even_if_ignored=False,
    task_track_started=False,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Kolkata",
    enable_utc=True,
    worker_prefetch_multiplier=1,   # one task at a time per worker
    broker_pool_limit=1,
    broker_connection_retry_on_startup=True,
    broker_transport_options={
        "polling_interval": 0,
        "visibility_timeout": 60 * 60,
        "health_check_interval": 0,
    },
)
