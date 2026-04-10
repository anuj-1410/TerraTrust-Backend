"""
Celery application — background task infrastructure.

Uses Redis as both broker and result backend.
"""

from celery import Celery

from app.config import settings

celery_app = Celery(
    "terratrust",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
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
    task_track_started=True,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Kolkata",
    enable_utc=True,
    worker_prefetch_multiplier=1,   # one task at a time per worker
)
