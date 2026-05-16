"""Celery application configuration."""

from celery import Celery
from celery.utils.log import get_task_logger
from config.settings import settings

logger = get_task_logger(__name__)

celery_app = Celery(
    "forensic_flight",
    broker=settings.celery_broker_url or settings.redis_url,
    backend=settings.celery_result_url or settings.redis_result_url,
    include=["api.workers.tasks"],
)

celery_app.conf.update(
    # Serialisation
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",

    # Time
    timezone="UTC",
    enable_utc=True,

    # Reliability
    task_track_started=True,
    task_acks_late=True,              # ack only after success; re-queue on worker crash
    worker_prefetch_multiplier=1,     # one task at a time — prevents starvation
    task_reject_on_worker_lost=True,  # nack (not ack) if worker dies mid-task

    # Result TTL — keep results 48h for debugging, then purge
    result_expires=172800,

    # Dead letter queue: failed tasks go to {queue}.dlq after max_retries exhausted.
    # DLQ keys are visible in Redis and can be inspected with:
    #   redis-cli LRANGE celery.parse.dlq 0 -1
    task_routes={
        "api.workers.tasks.parse_log_task": {
            "queue": "parse",
        },
        "api.workers.tasks.run_investigation_task": {
            "queue": "investigate",
        },
    },

    # Queues configuration with DLQ
    task_queues={
        "parse": {
            "exchange": "parse",
            "routing_key": "parse",
        },
        "investigate": {
            "exchange": "investigate",
            "routing_key": "investigate",
        },
        "parse.dlq": {
            "exchange": "parse.dlq",
            "routing_key": "parse.dlq",
        },
        "investigate.dlq": {
            "exchange": "investigate.dlq",
            "routing_key": "investigate.dlq",
        },
    },

    # Timeouts — investigations nominally complete in <90s; 429 retry storms can stretch
    # to ~20 min. Soft limit sends SIGTERM so the task can clean up DB state.
    # Hard limit sends SIGKILL as absolute backstop.
    task_soft_time_limit=900,    # SIGTERM at 15 min → SoftTimeLimitExceeded raised
    task_time_limit=1200,        # SIGKILL at 20 min → prevents zombie workers
)
