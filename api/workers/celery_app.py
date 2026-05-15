"""Celery application configuration."""

from celery import Celery
from config.settings import settings

celery_app = Celery(
    "forensic_flight",
    broker=settings.redis_url,
    backend=settings.redis_result_url,
    include=["api.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,   # One task at a time per worker
    task_routes={
        "api.workers.tasks.parse_log_task": {"queue": "parse"},
        "api.workers.tasks.run_investigation_task": {"queue": "investigate"},
    },
)
