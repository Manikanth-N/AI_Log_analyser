"""Celery tasks for log parsing and investigation."""

import uuid
from pathlib import Path

import structlog

from .celery_app import celery_app

log = structlog.get_logger(__name__)


# ── Parse task ────────────────────────────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="api.workers.tasks.parse_log_task",
    max_retries=2,
    default_retry_delay=30,
    # Hard limits set in celery_app.conf (task_soft_time_limit / task_time_limit)
)
def parse_log_task(self, flight_id: str, file_path: str):
    """
    Parse a raw log file and write Parquet + derived data.
    Runs on the 'parse' queue with 2 concurrent workers.

    Retries:
      - Transient failures (OOM, file lock): retry up to 2× with 30s delay.
      - Permanent failures (corrupt file, unknown format): no retry — mark error.
    """
    from config.settings import settings
    from pipeline.ingestion import IngestionPipeline
    from storage.metadata_db import MetadataDB

    db = MetadataDB()

    def progress(stage: str, pct: float):
        self.update_state(
            state="PROGRESS",
            meta={"stage": stage, "progress": pct, "flight_id": flight_id},
        )
        db.update_flight_status(flight_id, "parsing", progress_pct=int(pct * 100))

    try:
        db.update_flight_status(flight_id, "parsing")

        pipeline = IngestionPipeline(
            flight_id=flight_id,
            source_path=Path(file_path),
            progress_cb=progress,
        )
        result = pipeline.run()

        db.update_flight_status(
            flight_id,
            "ready",
            duration_s=result.metadata.duration_seconds,
            message_types=result.metadata.message_types,
            missing_critical=result.metadata.missing_critical,
            fw_version=result.metadata.firmware_version,
        )

        log.info("parse_complete", flight_id=flight_id, rows=result.rows_parsed)
        return {"flight_id": flight_id, "rows": result.rows_parsed, "status": "ready"}

    except (MemoryError, OSError) as exc:
        # Transient — retry
        log.warning("parse_transient_error", flight_id=flight_id, error=str(exc),
                    retries=self.request.retries)
        raise self.retry(exc=exc)

    except Exception as exc:
        # Permanent — mark error, route to DLQ
        log.error("parse_permanent_error", flight_id=flight_id, error=str(exc))
        db.update_flight_status(flight_id, "error")
        _send_to_dlq("parse.dlq", {
            "task": "parse_log_task",
            "flight_id": flight_id,
            "file_path": file_path,
            "error": str(exc),
        })
        raise


# ── Investigation task ────────────────────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="api.workers.tasks.run_investigation_task",
    max_retries=1,               # retry once — LLM transient failures are common
    default_retry_delay=60,
)
def run_investigation_task(self, investigation_id: str, flight_id: str, query: str):
    """
    Run complete multi-agent investigation.
    Runs on the 'investigate' queue with 1 concurrent worker.

    Retries:
      - Provider timeout / 429 / 5xx: retry once after 60s.
      - Permanent failures (invalid state, missing data): DLQ.
    """
    import redis
    from config.settings import settings
    from orchestrator.graph import InvestigationOrchestrator
    from storage.metadata_db import MetadataDB

    db = MetadataDB()
    redis_client = redis.Redis.from_url(settings.redis_pubsub_url)

    try:
        db.update_investigation(investigation_id, status="running")
        self.update_state(state="PROGRESS", meta={"investigation_id": investigation_id,
                                                   "stage": "running"})

        orchestrator = InvestigationOrchestrator(
            flight_id=flight_id,
            investigation_id=investigation_id,
            pubsub_client=redis_client,
        )

        final_state = orchestrator.run(
            user_query=query,
            max_iterations=3,
        )

        anomalies = final_state.get("anomalies", [])
        if anomalies:
            db.save_anomalies(flight_id, anomalies)

        db.update_investigation(
            investigation_id,
            status="complete",
            root_cause=final_state.get("root_cause"),
            confidence=final_state.get("confidence"),
            contributing_factors=final_state.get("contributing_factors", []),
            recommendations=final_state.get("recommendations", []),
            report_path=final_state.get("report_path"),
            iteration_count=final_state.get("iteration", 0),
            agent_findings=final_state.get("agent_findings", {}),
            open_questions=final_state.get("open_questions", []),
        )

        redis_client.publish(
            f"inv:{investigation_id}",
            '{"type": "complete", "data": {"status": "complete"}}',
        )

        log.info("investigation_complete", investigation_id=investigation_id,
                 classification=final_state.get("classification", "?"))
        return {"investigation_id": investigation_id, "status": "complete"}

    except Exception as exc:
        error_str = str(exc)
        is_transient = _is_transient_error(exc)

        if is_transient and self.request.retries < self.max_retries:
            log.warning(
                "investigation_transient_error",
                investigation_id=investigation_id,
                error=error_str,
                retries=self.request.retries,
            )
            db.update_investigation(investigation_id, status="retrying")
            raise self.retry(exc=exc)

        # Permanent failure
        log.error("investigation_failed", investigation_id=investigation_id, error=error_str)
        db.update_investigation(investigation_id, status="error")
        _send_to_dlq("investigate.dlq", {
            "task": "run_investigation_task",
            "investigation_id": investigation_id,
            "flight_id": flight_id,
            "query": query,
            "error": error_str,
        })
        try:
            redis_client.publish(
                f"inv:{investigation_id}",
                f'{{"type": "error", "data": {{"error": "{error_str[:200]}"}}}}',
            )
        except Exception:
            pass
        raise


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_transient_error(exc: Exception) -> bool:
    """
    Classify an exception as transient (retry-able) or permanent.
    LLM provider errors (rate limit, timeout, 5xx) are transient.
    """
    msg = str(exc).lower()
    return any(kw in msg for kw in (
        "timeout", "rate limit", "429", "503", "502", "connection", "temporarily"
    ))


def _send_to_dlq(queue: str, payload: dict) -> None:
    """Send a failed task payload to the dead-letter queue for manual inspection."""
    try:
        import json
        import redis
        from config.settings import settings
        r = redis.Redis.from_url(settings.redis_url)
        r.rpush(f"celery.{queue}", json.dumps(payload))
        log.info("task_sent_to_dlq", queue=queue, task=payload.get("task"))
    except Exception as e:
        log.error("dlq_write_failed", error=str(e))
