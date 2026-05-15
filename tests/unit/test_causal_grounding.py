"""
Phase 3D-prime regression tests — causal grounding of contributing factors.

Invariants enforced:
  1. validate_contributing_factors rejects factors with no evidence linkage
  2. Factors with valid evidence IDs are preserved exactly
  3. Partially matched factors are trimmed to only valid evidence IDs
  4. "vibration" factor rejected if VibrationAgent absent and no IMU rule fires
  5. GPS factors allowed when GPS anomaly cluster is present
  6. Unsupported factors move to open_questions, not silently retained
  7. CrashInvestigatorAgent emits structured contributing_factors_structured
  8. Serialized state["contributing_factors"] includes evidence citations
"""

from __future__ import annotations

import pytest
from llm.structured import ContributingFactor
from agents.crash_investigator import validate_contributing_factors


# ── validate_contributing_factors unit tests ──────────────────────────────────

def test_grounded_factor_passes():
    factors = [ContributingFactor(
        factor="GPS signal loss",
        supporting_evidence=["GPS_SAT_COUNT_DROP", "GPSIntegrityAgent"],
        confidence="HIGH",
    )]
    valid_ids = {"GPS_SAT_COUNT_DROP", "GPS_FIX_DROP", "GPSIntegrityAgent"}
    grounded, unsupported = validate_contributing_factors(factors, valid_ids)
    assert len(grounded) == 1
    assert len(unsupported) == 0
    assert grounded[0].factor == "GPS signal loss"
    assert set(grounded[0].supporting_evidence) == {"GPS_SAT_COUNT_DROP", "GPSIntegrityAgent"}


def test_empty_supporting_evidence_rejected():
    factors = [ContributingFactor(
        factor="Urban canyon multipath interference",
        supporting_evidence=[],   # no evidence — must be rejected
        confidence="MEDIUM",
    )]
    grounded, unsupported = validate_contributing_factors(factors, {"GPS_SAT_COUNT_DROP"})
    assert len(grounded) == 0
    assert len(unsupported) == 1
    assert unsupported[0].factor == "Urban canyon multipath interference"


def test_fabricated_evidence_id_rejected():
    factors = [ContributingFactor(
        factor="Vibration-induced GPS lane switch",
        supporting_evidence=["IMU_VIBRATION_SEVERE", "VIB_RMS_EXCEEDED"],  # not in valid set
        confidence="HIGH",
    )]
    valid_ids = {"GPS_SAT_COUNT_DROP", "EKF_LANE_SWITCH"}  # no vibration rules
    grounded, unsupported = validate_contributing_factors(factors, valid_ids)
    assert len(grounded) == 0
    assert len(unsupported) == 1


def test_partial_match_trims_to_valid_only():
    factors = [ContributingFactor(
        factor="GPS signal degradation causing navigation errors",
        supporting_evidence=["GPS_SAT_COUNT_DROP", "INVENTED_GPS_RULE"],
        confidence="MEDIUM",
    )]
    valid_ids = {"GPS_SAT_COUNT_DROP", "EKF_LANE_SWITCH"}
    grounded, unsupported = validate_contributing_factors(factors, valid_ids)
    assert len(grounded) == 1
    # Trimmed to only valid IDs
    assert grounded[0].supporting_evidence == ["GPS_SAT_COUNT_DROP"]
    assert "INVENTED_GPS_RULE" not in grounded[0].supporting_evidence


def test_multiple_factors_mixed_outcome():
    factors = [
        ContributingFactor(
            factor="GPS integrity collapse",
            supporting_evidence=["GPS_SAT_COUNT_DROP", "GPSIntegrityAgent"],
            confidence="HIGH",
        ),
        ContributingFactor(
            factor="Atmospheric multipath",
            supporting_evidence=["URBAN_CANYON_EFFECT"],  # invented
            confidence="LOW",
        ),
        ContributingFactor(
            factor="EKF divergence",
            supporting_evidence=["EKF_LANE_SWITCH"],
            confidence="HIGH",
        ),
    ]
    valid_ids = {"GPS_SAT_COUNT_DROP", "GPSIntegrityAgent", "EKF_LANE_SWITCH"}
    grounded, unsupported = validate_contributing_factors(factors, valid_ids)
    assert len(grounded) == 2
    assert len(unsupported) == 1
    assert unsupported[0].factor == "Atmospheric multipath"
    grounded_factors = {f.factor for f in grounded}
    assert "GPS integrity collapse" in grounded_factors
    assert "EKF divergence" in grounded_factors


def test_vibration_absent_when_no_vibration_evidence():
    """If VibrationAnalysisAgent is absent from valid_ids and no IMU rules fired,
    vibration-citing factors must be rejected."""
    vibration_factor = ContributingFactor(
        factor="Vibration-induced GPS lane switch",
        supporting_evidence=["VibrationAnalysisAgent"],
        confidence="HIGH",
    )
    # valid_ids contains GPS and EKF rules but NOT VibrationAnalysisAgent
    valid_ids = {"GPS_SAT_COUNT_DROP", "EKF_LANE_SWITCH", "GPSIntegrityAgent"}
    grounded, unsupported = validate_contributing_factors([vibration_factor], valid_ids)
    assert len(grounded) == 0
    assert len(unsupported) == 1


def test_vibration_allowed_when_vibration_agent_present():
    """If VibrationAnalysisAgent is in valid_ids (agent ran and found issues),
    vibration-citing factors are grounded."""
    vibration_factor = ContributingFactor(
        factor="Vibration-induced IMU contamination affecting sensor fusion",
        supporting_evidence=["VibrationAnalysisAgent", "IMU_RAW_EXTREME"],
        confidence="MEDIUM",
    )
    valid_ids = {"GPS_SAT_COUNT_DROP", "EKF_LANE_SWITCH", "VibrationAnalysisAgent",
                 "IMU_RAW_EXTREME"}
    grounded, unsupported = validate_contributing_factors([vibration_factor], valid_ids)
    assert len(grounded) == 1
    assert len(unsupported) == 0


def test_gps_factor_allowed_when_gps_anomaly_cluster_present():
    gps_factor = ContributingFactor(
        factor="GPS integrity collapse in satellite blackout zone",
        supporting_evidence=["GPS_SAT_COUNT_DROP", "GPS_FIX_DROP", "GPS_POSITION_GLITCH"],
        confidence="HIGH",
    )
    valid_ids = {"GPS_SAT_COUNT_DROP", "GPS_FIX_DROP", "GPS_POSITION_GLITCH",
                 "EKF_LANE_SWITCH"}
    grounded, unsupported = validate_contributing_factors([gps_factor], valid_ids)
    assert len(grounded) == 1
    assert set(grounded[0].supporting_evidence) == {
        "GPS_SAT_COUNT_DROP", "GPS_FIX_DROP", "GPS_POSITION_GLITCH"
    }


def test_all_factors_unsupported_returns_empty_grounded():
    factors = [
        ContributingFactor(factor="Wind shear", supporting_evidence=[], confidence="LOW"),
        ContributingFactor(factor="Pilot error", supporting_evidence=["PILOT_INPUT_ANOMALY"],
                           confidence="LOW"),
    ]
    valid_ids = {"GPS_SAT_COUNT_DROP", "EKF_LANE_SWITCH"}
    grounded, unsupported = validate_contributing_factors(factors, valid_ids)
    assert grounded == []
    assert len(unsupported) == 2


# ── CrashInvestigatorAgent integration ───────────────────────────────────────

def _make_crash_agent():
    from agents.crash_investigator import CrashInvestigatorAgent
    from unittest.mock import MagicMock
    import structlog

    store = MagicMock()
    store.read_derived.return_value = {}

    agent = CrashInvestigatorAgent.__new__(CrashInvestigatorAgent)
    agent.flight_id = "test-flight"
    agent.investigation_id = "test-inv"
    agent.store = store
    agent.llm = MagicMock()
    agent.llm.primary_model = "test"
    agent.log = structlog.get_logger("test")
    return agent


def _minimal_state(anomalies=None, agent_findings=None):
    return {
        "flight_id": "test-flight",
        "investigation_id": "test-inv",
        "user_query": "Test investigation",
        "anomalies": anomalies or [],
        "hypotheses": [],
        "agent_findings": agent_findings or {},
        "messages": [],
        "errors": [],
    }


def test_crash_agent_unsupported_factors_moved_to_open_questions():
    from llm.structured import (
        CrashInvestigationResult, HypothesisRecord, CorrectiveAction
    )

    unsupported_result = CrashInvestigationResult(
        proximate_cause="GPS failure",
        root_causes=[],
        contributing_factors=[
            ContributingFactor(
                factor="Unsupported urban canyon claim",
                supporting_evidence=[],  # no evidence
                confidence="LOW",
            ),
        ],
        refuted_hypotheses=[],
        missing_evidence=[],
        causal_chain="GPS → EKF → CRASH",
        five_why=["Why 1"],
        overall_confidence=0.70,
        confidence_label="MEDIUM",
        corrective_actions=[],
        open_questions=["Original question"],
    )

    agent = _make_crash_agent()
    agent.llm.structured.return_value = unsupported_result

    real_anomalies = [
        {"rule_name": "GPS_SAT_COUNT_DROP", "timestamp_us": 100_000_000,
         "severity": "CRITICAL", "category": "GPS",
         "description": "sat drop", "raw_values": {}},
    ]
    state = _minimal_state(anomalies=real_anomalies)
    result = agent.run(state)

    # Unsupported factor should NOT appear in contributing_factors
    assert result["contributing_factors"] == [], (
        f"Unsupported factor survived validation: {result['contributing_factors']}"
    )
    # Must appear in open_questions
    oq_text = " ".join(result["open_questions"])
    assert "Unsupported claim" in oq_text or "urban canyon" in oq_text.lower(), (
        f"Unsupported factor not in open_questions: {result['open_questions']}"
    )


def test_crash_agent_grounded_factors_preserved_with_citations():
    from llm.structured import CrashInvestigationResult

    grounded_result = CrashInvestigationResult(
        proximate_cause="GPS failure",
        root_causes=[],
        contributing_factors=[
            ContributingFactor(
                factor="GPS integrity collapse",
                supporting_evidence=["GPS_SAT_COUNT_DROP", "GPSIntegrityAgent"],
                confidence="HIGH",
            ),
        ],
        refuted_hypotheses=[],
        missing_evidence=[],
        causal_chain="GPS → EKF → CRASH",
        five_why=["Why 1"],
        overall_confidence=0.85,
        confidence_label="HIGH",
        corrective_actions=[],
        open_questions=[],
    )

    agent = _make_crash_agent()
    agent.llm.structured.return_value = grounded_result

    real_anomalies = [
        {"rule_name": "GPS_SAT_COUNT_DROP", "timestamp_us": 100_000_000,
         "severity": "CRITICAL", "category": "GPS",
         "description": "sat drop", "raw_values": {}},
    ]
    state = _minimal_state(
        anomalies=real_anomalies,
        agent_findings={"GPSIntegrityAgent": {"summary": "GPS failure confirmed"}},
    )
    result = agent.run(state)

    assert len(result["contributing_factors"]) == 1
    cf_str = result["contributing_factors"][0]
    assert "GPS integrity collapse" in cf_str
    assert "GPS_SAT_COUNT_DROP" in cf_str  # evidence citation in serialized form
    assert "GPSIntegrityAgent" in cf_str

    # Structured form preserved in agent_findings
    structured = result["agent_findings"]["CrashInvestigatorAgent"][
        "contributing_factors_structured"
    ]
    assert len(structured) == 1
    assert structured[0]["factor"] == "GPS integrity collapse"
    assert "GPS_SAT_COUNT_DROP" in structured[0]["supporting_evidence"]


def test_crash_agent_rejects_fabricated_rule_name():
    from llm.structured import CrashInvestigationResult

    fabricated_result = CrashInvestigationResult(
        proximate_cause="GPS failure",
        root_causes=[],
        contributing_factors=[
            ContributingFactor(
                factor="Vibration induced GPS failure",
                supporting_evidence=["EK3_POSITION_DRIFT", "IMU_VIBRATION_EXCESSIVE"],
                confidence="HIGH",
            ),
        ],
        refuted_hypotheses=[],
        missing_evidence=[],
        causal_chain="GPS → CRASH",
        five_why=["Why 1"],
        overall_confidence=0.70,
        confidence_label="MEDIUM",
        corrective_actions=[],
        open_questions=[],
    )

    agent = _make_crash_agent()
    agent.llm.structured.return_value = fabricated_result

    # State has only GPS anomalies — no vibration or EK3_POSITION_DRIFT
    real_anomalies = [
        {"rule_name": "GPS_SAT_COUNT_DROP", "timestamp_us": 100_000_000,
         "severity": "CRITICAL", "category": "GPS",
         "description": "sat drop", "raw_values": {}},
    ]
    state = _minimal_state(anomalies=real_anomalies)
    result = agent.run(state)

    # Factor cites only fabricated IDs not in valid set → rejected
    assert result["contributing_factors"] == [], (
        f"Fabricated-evidence factor survived: {result['contributing_factors']}"
    )
