"""Separate Celery application: institution workers never consume farmer queues."""
from celery import Celery
from app.config import settings
from app.redis_utils import secure_redis_url

institution_app = Celery("terratrust_institution", broker=secure_redis_url(settings.REDIS_URL), include=["tasks.institution_tasks"], set_as_current=False)
institution_app.conf.update(task_default_queue="tt_institution_operations", task_serializer="json", accept_content=["json"], task_ignore_result=True,
    worker_prefetch_multiplier=1, task_acks_late=True, task_reject_on_worker_lost=True, timezone="UTC", enable_utc=True,
    broker_connection_timeout=5, broker_pool_limit=2, task_publish_retry=False,
    broker_transport_options={"visibility_timeout": 25200, "socket_connect_timeout": 5, "socket_timeout": 5, "retry_on_timeout": False}, beat_schedule={
        "institution-dispatch": {"task": "institution.dispatch", "schedule": 5.0, "options": {"queue": "tt_institution_operations"}},
        "institution-watchdog": {"task": "institution.watchdog", "schedule": 30.0, "options": {"queue": "tt_institution_operations"}}})
