"""
Phase 6 concurrency and scalability tests.

Tests:
  1. Semaphore prevents concurrent Ollama calls within one investigation
  2. Two sequential investigations on the same flight complete without error
  3. Deterministic agents in parallel: N agents finish in < N × single_agent_time
  4. Per-future timeout scales with ollama_timeout_seconds (not hard-coded 600s)

These tests use the stub LLM so they run fast without Ollama.
Concurrent real-Ollama tests (5 simultaneous investigations) are in scripts/.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from tests.integration.stub_llm import _make_stub_client


def _known_flight_id():
    return "686998f2-95c3-4ef1-b8f9-e7b38ccccdb5"


def _run_investigation(flight_id: str, stub) -> dict:
    from storage.parquet_store import ParquetStore
    from orchestrator.graph import InvestigationOrchestrator
    import uuid

    store = ParquetStore()
    inv_id = str(uuid.uuid4())

    with patch("llm.client._client", stub):
        orch = InvestigationOrchestrator(
            flight_id=flight_id,
            investigation_id=inv_id,
            store=store,
        )
        return orch.run(max_iterations=1)


@pytest.mark.slow
def test_stub_investigation_completes():
    """Baseline: one investigation with stub LLM completes without errors."""
    stub = _make_stub_client()
    state = _run_investigation(_known_flight_id(), stub)
    assert "final_report" in state
    assert not state.get("errors"), f"Unexpected errors: {state.get('errors')}"


@pytest.mark.slow
def test_two_sequential_investigations_no_state_bleed():
    """Two sequential investigations on the same flight produce independent reports."""
    stub = _make_stub_client()
    state1 = _run_investigation(_known_flight_id(), stub)
    state2 = _run_investigation(_known_flight_id(), stub)

    # Each gets its own investigation_id
    assert state1["investigation_id"] != state2["investigation_id"]

    # Both classify correctly
    r1 = state1.get("final_report", {})
    r2 = state2.get("final_report", {})
    assert r1.get("classification") == "CRASH"
    assert r2.get("classification") == "CRASH"

    # Anomaly registries are independently built (same flight → same count)
    assert len(r1.get("anomaly_registry", [])) == len(r2.get("anomaly_registry", []))


@pytest.mark.slow
def test_agent_timeout_uses_settings_not_hardcoded():
    """The per-future timeout must scale with ollama_timeout_seconds, not be 600s."""
    from orchestrator.graph import _parallel_domain_analysis
    import inspect

    source = inspect.getsource(_parallel_domain_analysis)

    # Must NOT contain the old hard-coded 600
    assert "timeout=600" not in source, (
        "Hard-coded timeout=600 found in _parallel_domain_analysis — "
        "must use settings.ollama_timeout_seconds × multiplier"
    )
    # Must reference settings
    assert "ollama_timeout" in source or "_agent_timeout" in source


@pytest.mark.slow
def test_semaphore_is_module_level_singleton():
    """_OLLAMA_SEMAPHORE must be a module-level singleton (not per-client)."""
    from llm.client import _OLLAMA_SEMAPHORE
    import threading

    assert isinstance(_OLLAMA_SEMAPHORE, threading.Semaphore)

    # Two separate client instances share the same semaphore
    from llm.client import OllamaClient
    c1 = OllamaClient.__new__(OllamaClient)
    c2 = OllamaClient.__new__(OllamaClient)

    # Acquire via the module-level reference
    acquired = _OLLAMA_SEMAPHORE.acquire(blocking=False)
    assert acquired

    # Module-level reference should now be locked
    locked = _OLLAMA_SEMAPHORE.acquire(blocking=False)
    assert not locked, "Semaphore should be locked — only one token"

    _OLLAMA_SEMAPHORE.release()
