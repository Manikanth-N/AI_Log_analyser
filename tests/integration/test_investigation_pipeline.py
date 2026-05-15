"""
Integration test: parse → investigate → report.

Two variants:
  test_investigation_stub_llm   — stub LLM, runs in ~5 min, is the default regression gate
  test_investigation_live_llm   — real Ollama, ~90 min on CPU, gated by INTEGRATION_LIVE_LLM=1

Both variants call Celery task functions directly (.apply()) — no separate worker
process is required.  They exercise the full code path:

  parse_log_task.apply()
    → IngestionPipeline → Parquet + derived JSON + DB flight record

  run_investigation_task.apply()
    → InvestigationOrchestrator → LangGraph state machine
    → 11+ agents (deterministic rules + LLM structured calls)
    → DB investigation record + confidence + agent_findings
    → report_{inv_id}.json written to Parquet derived dir

Post-conditions checked:
  - investigation.status == "complete"
  - investigation.root_cause is non-empty
  - investigation.confidence in {LOW, MEDIUM, HIGH, DEFINITIVE}
  - investigation.contributing_factors is non-empty list
  - investigation.recommendations is non-empty list
  - investigation.agent_findings has entries for at least 5 agents
  - report JSON exists on disk
  - report contains: classification, confidence_level, corrective_actions
  - report classification == "CRASH" (known for 00000006.BIN)
  - anomalies written to DB (deterministic rules should fire regardless of LLM)

Environment:
  INTEGRATION_LIVE_LLM=1  run the live_llm variant (default: skipped)
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

from tests.integration.conftest import FIXTURE_LOG
from tests.integration.stub_llm import stub_llm  # noqa: F401 – imported for fixture


# ── expected outcomes for 00000006.BIN ───────────────────────────────────────

EXPECTED_CLASSIFICATION = "CRASH"
VALID_CONFIDENCE_LABELS = {"LOW", "MEDIUM", "HIGH", "DEFINITIVE"}
MIN_AGENTS_IN_FINDINGS = 5
MIN_ANOMALIES = 1


# ── shared setup / assertions ─────────────────────────────────────────────────

def _run_parse(flight_id: str, fixture_path: Path) -> None:
    """Run the parse task synchronously in-process."""
    from api.workers.tasks import parse_log_task

    result = parse_log_task.apply(args=[flight_id, str(fixture_path)])
    if result.failed():
        raise RuntimeError(f"parse_log_task failed: {result.traceback}")
    assert result.result["status"] == "ready", (
        f"Parse did not reach 'ready': {result.result}"
    )


def _run_investigation(investigation_id: str, flight_id: str) -> None:
    """Run the investigation task synchronously in-process."""
    from api.workers.tasks import run_investigation_task

    result = run_investigation_task.apply(args=[
        investigation_id,
        flight_id,
        "Perform complete forensic investigation of this flight log",
    ])
    if result.failed():
        raise RuntimeError(f"run_investigation_task failed: {result.traceback}")


def _assert_investigation_complete(
    investigation_id: str,
    flight_id: str,
) -> None:
    """Assert all post-conditions on DB + disk after investigation completes."""
    from config.settings import settings
    from storage.metadata_db import MetadataDB

    db = MetadataDB()

    # ── DB: investigation record ──────────────────────────────────────────────
    inv = db.get_investigation(investigation_id)
    assert inv is not None, "Investigation record not found in DB"
    assert inv.status == "complete", f"investigation.status={inv.status!r}"

    assert inv.root_cause, "root_cause is empty"
    assert inv.confidence in VALID_CONFIDENCE_LABELS, (
        f"confidence={inv.confidence!r} not in {VALID_CONFIDENCE_LABELS}"
    )
    assert inv.contributing_factors, "contributing_factors is empty"
    assert inv.recommendations, "recommendations is empty"

    agent_findings = inv.agent_findings or {}
    assert len(agent_findings) >= MIN_AGENTS_IN_FINDINGS, (
        f"Only {len(agent_findings)} agents in findings; expected ≥{MIN_AGENTS_IN_FINDINGS}"
    )

    # ── DB: anomalies ─────────────────────────────────────────────────────────
    anomalies = db.get_anomalies(flight_id)
    assert len(anomalies) >= MIN_ANOMALIES, (
        f"Only {len(anomalies)} anomalies in DB; expected ≥{MIN_ANOMALIES}"
    )

    # ── Disk: report JSON ─────────────────────────────────────────────────────
    report_path = (
        settings.flights_storage
        / flight_id
        / "derived"
        / f"report_{investigation_id}.json"
    )
    assert report_path.exists(), f"Report file not written: {report_path}"

    import json
    report = json.loads(report_path.read_text())

    assert "classification" in report, "report missing 'classification'"
    assert "confidence_level" in report, "report missing 'confidence_level'"
    assert "corrective_actions" in report, "report missing 'corrective_actions'"
    assert report["corrective_actions"], "corrective_actions is empty in report"
    assert "root_cause_determination" in report, "report missing 'root_cause_determination'"

    # ── Classification matches expected fixture outcome ────────────────────────
    assert report["classification"] == EXPECTED_CLASSIFICATION, (
        f"Expected classification={EXPECTED_CLASSIFICATION!r}, "
        f"got {report['classification']!r}"
    )


# ── test: stub LLM (default regression gate) ──────────────────────────────────

def test_investigation_stub_llm(stub_llm, tracked_flight_ids):  # noqa: F811
    """
    Full pipeline with StubOllamaClient.

    Exercises: parse → orchestrator → all agents → DB write → report JSON.
    LLM calls return canned valid Pydantic instances — no Ollama needed.
    ~5 min on first run (parse), <30 s investigation with stub.
    """
    if not FIXTURE_LOG.exists():
        pytest.skip(f"Fixture log not found: {FIXTURE_LOG}")

    flight_id = str(uuid.uuid4())
    investigation_id = str(uuid.uuid4())
    tracked_flight_ids.append(flight_id)

    # Create a copy of the fixture so the raw path is valid and isolated
    from config.settings import settings

    upload_dir = settings.raw_storage / flight_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest = upload_dir / FIXTURE_LOG.name
    shutil.copy2(FIXTURE_LOG, dest)

    # Insert stub flight record so investigation task can look it up
    from storage.metadata_db import MetadataDB
    db = MetadataDB()
    db.create_flight(
        id=uuid.UUID(flight_id),
        sha256="stub-" + flight_id[:8],
        filename=FIXTURE_LOG.name,
        file_size=FIXTURE_LOG.stat().st_size,
        raw_path=str(dest),
        status="uploaded",
    )

    # 1. Parse (real IngestionPipeline, no LLM)
    _run_parse(flight_id, dest)

    # Verify parse outcome before proceeding
    flight = db.get_flight(flight_id)
    assert flight.status == "ready", f"parse status={flight.status}"

    # 2. Create investigation record
    inv = db.create_investigation(flight_id=flight_id, query="Forensic investigation (stub LLM)")
    investigation_id = str(inv.id)

    # 3. Run investigation (LLM calls intercepted by stub_llm fixture)
    _run_investigation(investigation_id, flight_id)

    # 4. Assert all post-conditions
    _assert_investigation_complete(investigation_id, flight_id)


# ── test: live LLM (run explicitly, ~90 min on CPU) ──────────────────────────

@pytest.mark.live_llm
@pytest.mark.slow
def test_investigation_live_llm(tracked_flight_ids):
    """
    Full pipeline with real Ollama inference.

    Requires INTEGRATION_LIVE_LLM=1 and an Ollama server with the model
    specified by OLLAMA_PRIMARY_MODEL (defaults to qwen3:8b-q4_K_M via .env).

    On RTX 3050 / CPU-only: expect ~60-90 min total.
    """
    if not os.environ.get("INTEGRATION_LIVE_LLM"):
        pytest.skip("Set INTEGRATION_LIVE_LLM=1 to run live LLM integration test")

    if not FIXTURE_LOG.exists():
        pytest.skip(f"Fixture log not found: {FIXTURE_LOG}")

    # Verify Ollama is reachable before committing to a 90-min run
    from llm.client import get_llm_client
    if not get_llm_client().check_health():
        pytest.skip("Ollama not reachable or model not available")

    flight_id = str(uuid.uuid4())
    tracked_flight_ids.append(flight_id)

    from config.settings import settings
    from storage.metadata_db import MetadataDB

    upload_dir = settings.raw_storage / flight_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest = upload_dir / FIXTURE_LOG.name
    shutil.copy2(FIXTURE_LOG, dest)

    db = MetadataDB()
    db.create_flight(
        id=uuid.UUID(flight_id),
        sha256="live-" + flight_id[:8],
        filename=FIXTURE_LOG.name,
        file_size=FIXTURE_LOG.stat().st_size,
        raw_path=str(dest),
        status="uploaded",
    )

    _run_parse(flight_id, dest)

    flight = db.get_flight(flight_id)
    assert flight.status == "ready"

    inv = db.create_investigation(flight_id=flight_id, query="Forensic investigation (live LLM)")
    investigation_id = str(inv.id)

    _run_investigation(investigation_id, flight_id)
    _assert_investigation_complete(investigation_id, flight_id)
