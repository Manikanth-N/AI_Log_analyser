"""Celery tasks for log parsing and investigation."""

import uuid
from pathlib import Path

import structlog

from .celery_app import celery_app

log = structlog.get_logger(__name__)


@celery_app.task(bind=True, max_retries=0, time_limit=3600, name="api.workers.tasks.parse_log_task")
def parse_log_task(self, flight_id: str, file_path: str):
    """
    Parse a raw log file and write Parquet + derived data.
    Runs on the 'parse' queue with 2 concurrent workers.
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

        # Update flight record with metadata
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

    except Exception as e:
        log.error("parse_error", flight_id=flight_id, error=str(e))
        db.update_flight_status(flight_id, "error")
        raise


@celery_app.task(bind=True, max_retries=0, time_limit=7200, name="api.workers.tasks.run_investigation_task")
def run_investigation_task(self, investigation_id: str, flight_id: str, query: str):
    """
    Run complete multi-agent investigation.
    Runs on the 'investigate' queue with 1 concurrent worker (GPU constraint).
    """
    import redis
    from config.settings import settings
    from orchestrator.graph import InvestigationOrchestrator
    from storage.metadata_db import MetadataDB

    db = MetadataDB()

    redis_client = redis.Redis.from_url(settings.redis_pubsub_url)

    try:
        db.update_investigation(investigation_id, status="running")

        orchestrator = InvestigationOrchestrator(
            flight_id=flight_id,
            investigation_id=investigation_id,
            pubsub_client=redis_client,
        )

        final_state = orchestrator.run(
            user_query=query,
            max_iterations=3,
        )

        # Persist anomalies to DB
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

        # Signal completion
        redis_client.publish(
            f"inv:{investigation_id}",
            '{"type": "complete", "data": {"status": "complete"}}',
        )

        return {"investigation_id": investigation_id, "status": "complete"}

    except Exception as e:
        log.error("investigation_error", investigation_id=investigation_id, error=str(e))
        db.update_investigation(investigation_id, status="error")
        redis_client.publish(
            f"inv:{investigation_id}",
            f'{{"type": "error", "data": {{"error": "{str(e)}"}}}}',
        )
        raise
