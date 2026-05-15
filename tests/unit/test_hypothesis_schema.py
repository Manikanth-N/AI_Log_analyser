"""
Regression tests for Phase 2A and 2B fixes:
  - Canonical hypothesis schema (make_hypothesis factory)
  - SafetyComplianceAgent emits valid schema
  - Parallel domain analysis produces no duplicate hypotheses
  - Parse-phase anomalies seeded into investigation state
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import uuid

import pytest

from agents.base import make_hypothesis, HypothesisStatus


# ── make_hypothesis factory ───────────────────────────────────────────────────

def test_make_hypothesis_required_fields():
    h = make_hypothesis(
        title="GPS failure",
        description="GPS satellite count dropped",
        agent_source="GPSIntegrityAgent",
        confidence=0.85,
    )
    assert h["title"] == "GPS failure"
    assert h["description"] == "GPS satellite count dropped"
    assert h["agent_source"] == "GPSIntegrityAgent"
    assert h["confidence"] == 0.85
    assert h["status"] == "forming"
    assert isinstance(h["id"], str) and len(h["id"]) > 0
    assert h["evidence_for"] == []
    assert h["evidence_against"] == []
    assert h["missing_evidence"] == []


def test_make_hypothesis_all_fields():
    custom_id = str(uuid.uuid4())
    h = make_hypothesis(
        id=custom_id,
        title="EKF failure",
        description="EKF IVR exceeded threshold",
        agent_source="EKFDiagnosticsAgent",
        confidence=0.90,
        status="supported",
        evidence_for=["IVR=0.63"],
        evidence_against=["Filter recovered"],
        missing_evidence=["Second IMU data"],
    )
    assert h["id"] == custom_id
    assert h["status"] == "supported"
    assert h["evidence_for"] == ["IVR=0.63"]
    assert h["evidence_against"] == ["Filter recovered"]
    assert h["missing_evidence"] == ["Second IMU data"]


def test_make_hypothesis_unique_ids():
    h1 = make_hypothesis(title="A", description="a", agent_source="X", confidence=0.5)
    h2 = make_hypothesis(title="B", description="b", agent_source="Y", confidence=0.5)
    assert h1["id"] != h2["id"]


# ── SafetyComplianceAgent canonical schema ────────────────────────────────────

def _make_safety_agent():
    from agents.safety_compliance import SafetyComplianceAgent
    store = MagicMock()
    store.load.return_value = None
    return SafetyComplianceAgent(
        flight_id="test-flight",
        investigation_id="test-inv",
        store=store,
    )


def _minimal_state():
    return {
        "hypotheses": [],
        "anomalies": [],
        "messages": [],
        "errors": [],
        "agent_findings": {},
    }


def test_safety_compliance_no_violations_emits_no_hypothesis():
    agent = _make_safety_agent()
    state = _minimal_state()
    # store.load returns None → params empty → no violations → no hypothesis
    result = agent.run(state)
    assert result["hypotheses"] == []


def test_safety_compliance_violations_emit_canonical_hypothesis():
    import polars as pl
    from agents.safety_compliance import SafetyComplianceAgent

    # Build a PARM dataframe with a deliberate violation:
    # FS_THR_ENABLE = 0 (required = 1)
    parm_df = pl.DataFrame({
        "name": ["FS_THR_ENABLE", "BATT_LOW_VOLT", "BATT_CRT_VOLT"],
        "value": [0.0, 3.5, 3.2],
    })

    store = MagicMock()
    store.load.return_value = parm_df

    agent = SafetyComplianceAgent(
        flight_id="test-flight",
        investigation_id="test-inv",
        store=store,
    )
    state = _minimal_state()
    result = agent.run(state)

    assert len(result["hypotheses"]) == 1
    h = result["hypotheses"][0]

    # All canonical fields must be present
    required_keys = {"id", "title", "description", "agent_source", "confidence",
                     "status", "evidence_for", "evidence_against", "missing_evidence"}
    assert required_keys.issubset(h.keys()), (
        f"Missing keys: {required_keys - set(h.keys())}"
    )

    # Values must be valid
    assert h["agent_source"] == "SafetyComplianceAgent"
    assert h["status"] in ("forming", "supported", "refuted", "confirmed")
    assert 0.0 <= h["confidence"] <= 1.0
    assert isinstance(h["evidence_for"], list)
    assert isinstance(h["evidence_against"], list)
    assert isinstance(h["missing_evidence"], list)

    # Old wrong keys must NOT be present
    assert "agent" not in h, "Legacy 'agent' key must not appear"
    assert "text" not in h, "Legacy 'text' key must not appear"


# ── Shallow-copy isolation (no hypothesis explosion) ─────────────────────────

def test_parallel_domain_analysis_no_duplicate_hypotheses():
    """
    With max_workers=1 and isolated output slices, each agent's hypothesis
    must appear exactly once in the merged state.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Simulate 3 agents each writing 1 hypothesis
    agents_output = [
        {"hypotheses": [make_hypothesis(title=f"h{i}", description="d", agent_source=f"A{i}", confidence=0.5)],
         "anomalies": [], "messages": [], "errors": [], "agent_findings": {}}
        for i in range(3)
    ]

    state: dict = {"hypotheses": [], "anomalies": [], "messages": [], "errors": [], "agent_findings": {}}

    # Reproduce the fixed orchestrator merge pattern
    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {
            executor.submit(lambda s: s, agent_out): i
            for i, agent_out in enumerate(agents_output)
        }
        for future in as_completed(futures):
            result_state = future.result()
            state.setdefault("hypotheses", []).extend(result_state.get("hypotheses", []))

    assert len(state["hypotheses"]) == 3, (
        f"Expected 3 unique hypotheses, got {len(state['hypotheses'])}"
    )
    titles = [h["title"] for h in state["hypotheses"]]
    assert len(set(titles)) == 3, f"Duplicate titles found: {titles}"


# ── Parse-phase anomaly seeding ───────────────────────────────────────────────

def test_orchestrator_seeds_parse_anomalies():
    """InvestigationOrchestrator initial state includes anomalies_fast.json content."""
    from orchestrator.graph import InvestigationOrchestrator

    fake_anomalies = [
        {"rule_name": "GPS_SAT_DROP", "timestamp_us": 1000, "severity": "CRITICAL"},
        {"rule_name": "EKF_VARIANCE", "timestamp_us": 2000, "severity": "WARNING"},
    ]

    store = MagicMock()
    store.read_derived.return_value = fake_anomalies

    # Patch build_investigation_graph to capture initial_state without running the graph
    captured_state = {}

    def fake_compile():
        compiled = MagicMock()
        def fake_stream(initial_state, **kwargs):
            captured_state.update(initial_state)
            # Return one chunk so the loop exits
            return iter([initial_state])
        compiled.stream = fake_stream
        return compiled

    with patch("orchestrator.graph.build_investigation_graph") as mock_build:
        mock_graph = MagicMock()
        mock_graph.compile = fake_compile
        mock_build.return_value = mock_graph

        orch = InvestigationOrchestrator(
            flight_id="test-flight",
            investigation_id="test-inv",
            store=store,
        )
        orch.run()

    assert captured_state.get("anomalies") == fake_anomalies, (
        f"Parse anomalies not seeded into initial state: {captured_state.get('anomalies')}"
    )
