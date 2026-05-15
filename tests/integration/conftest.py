"""
Integration test fixtures.

Two test tiers:
  - parse_pipeline   : upload → API → Celery parse worker → ready status
                       Requires: API server + parse worker running
  - investigation    : parse_log_task.apply() + run_investigation_task.apply()
                       Requires: PostgreSQL + Redis (no separate worker process)
                       LLM:      StubOllamaClient by default (~5 min)
                                 Real Ollama when INTEGRATION_LIVE_LLM=1 (~90 min)

Environment variables:
  INTEGRATION_API_URL  base URL of the API server (default: http://localhost:8001)
  INTEGRATION_LIVE_LLM set to 1 to use real Ollama inference (skipped by default)
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Callable

import httpx
import pytest

# ── constants ────────────────────────────────────────────────────────────────

API_URL = os.environ.get("INTEGRATION_API_URL", "http://localhost:8001")

FIXTURE_LOG = Path(__file__).parent.parent.parent / "logs" / "00000006.BIN"

PARSE_TIMEOUT_S = 360       # 6 min — 29 MB BIN on fast SSD
INVESTIGATION_TIMEOUT_S = 5_400  # 90 min — CPU-only Ollama worst case
POLL_INTERVAL_S = 5


# ── helpers ──────────────────────────────────────────────────────────────────

def poll_until(
    condition: Callable[[], bool],
    timeout_s: float,
    interval_s: float = POLL_INTERVAL_S,
    description: str = "condition",
) -> None:
    """Block until condition() returns True or timeout elapses."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return
        remaining = deadline - time.monotonic()
        time.sleep(min(interval_s, max(0, remaining)))
    raise TimeoutError(f"Timed out after {timeout_s}s waiting for: {description}")


# ── session-scoped fixtures ───────────────────────────────────────────────────

@pytest.fixture(scope="session")
def fixture_log() -> Path:
    if not FIXTURE_LOG.exists():
        pytest.skip(f"Fixture log not found: {FIXTURE_LOG}")
    return FIXTURE_LOG


@pytest.fixture(scope="session")
def api_client() -> httpx.Client:
    """httpx client pointed at the API server. Skips if server unreachable."""
    try:
        with httpx.Client(base_url=API_URL, timeout=10) as probe:
            probe.get("/api/v1/health").raise_for_status()
    except Exception as exc:
        pytest.skip(f"API server not reachable at {API_URL}: {exc}")

    client = httpx.Client(base_url=API_URL, timeout=30)
    yield client
    client.close()


# ── per-test flight cleanup ───────────────────────────────────────────────────

@pytest.fixture()
def tracked_flight_ids():
    """
    Accumulate flight IDs created during a test; delete them (DB + Parquet) on teardown.
    Use: tracked_flight_ids.append(flight_id)
    """
    created: list[str] = []
    yield created

    from config.settings import settings
    from storage.metadata_db import MetadataDB

    db = MetadataDB()
    for fid in created:
        try:
            db.delete_flight(fid)
        except Exception:
            pass  # best-effort
        flight_dir = settings.flights_storage / fid
        if flight_dir.exists():
            shutil.rmtree(flight_dir, ignore_errors=True)
        raw_dir = settings.raw_storage / fid
        if raw_dir.exists():
            shutil.rmtree(raw_dir, ignore_errors=True)
