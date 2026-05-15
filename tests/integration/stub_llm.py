"""
StubOllamaClient — returns hard-coded valid Pydantic instances for every
structured() call so the full agent/orchestrator/storage pipeline runs
without hitting Ollama.

Agents that call the LLM:
  EKFDiagnosticsAgent    → EKFDiagnosticResult
  GPSIntegrityAgent      → GPSIntegrityResult
  PowerSystemAgent       → PowerSystemResult
  VibrationAnalysisAgent → VibrationResult
  CrashInvestigatorAgent → CrashInvestigationResult
  ReportWriterAgent      → ForensicReportLLM (anomaly_registry injected post-hoc)
  ComparativeAnalystAgent→ embed() only

All other agents (FlightTimeline, ESCMotor, FlightDynamics, MissionBehavior,
ParameterDrift, SafetyCompliance) are purely deterministic — no LLM calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from llm.inference_client import InferenceClient
from llm.structured import (
    ContributingFactor,
    CorrectiveAction,
    CrashInvestigationResult,
    EKFDiagnosticResult,
    ForensicReportLLM,
    GPSIntegrityResult,
    HypothesisRecord,
    PowerSystemResult,
    VibrationResult,
)

# ── canned responses ──────────────────────────────────────────────────────────

_EKF = EKFDiagnosticResult(
    filter_health="STRESSED",
    primary_failure_mode="GPS velocity innovation exceeded IVR threshold",
    innovation_ratio_max=0.63,
    lane_switch_occurred=True,
    lane_switch_timestamp_us=110_000_000,
    filter_recovered=False,
    position_trustworthy_after_event=False,
    magnetic_anomaly=False,
    height_estimate_reliable=True,
    confidence=0.85,
    summary="EKF stressed by GPS integrity failure; lane switch at T+110s.",
    causal_sensor="GPS",
)

_GPS = GPSIntegrityResult(
    integrity_score=18.0,
    glitches_detected=3,
    max_glitch_magnitude_m=12.5,
    hdop_min=1.2,
    hdop_max=4.8,
    hdop_at_failure=4.8,
    sat_count_min=4,
    sat_count_at_failure=4,
    spoofing_likelihood="NONE",
    spoofing_indicators=[],
    degradation_start_us=100_000_000,
    confidence=0.90,
    summary="GPS integrity collapse: sat count dropped from 18 to 4, HDOP spiked.",
    causal_to_ekf_failure=True,
)

_POWER = PowerSystemResult(
    voltage_at_failure_v=22.8,
    min_voltage_v=22.8,
    max_current_a=28.5,
    estimated_r_internal_ohm=0.045,
    brownout_detected=True,
    brownout_timestamp_us=108_000_000,
    current_spikes=2,
    battery_soc_at_failure_pct=35.0,
    failsafe_triggered=False,
    power_causal_to_failure=True,
    confidence=0.78,
    summary="Minor brownout detected; power contributed to EKF instability.",
)

_VIB = VibrationResult(
    rms_x=0.12,
    rms_y=0.14,
    rms_z=0.09,
    overall_severity="ACCEPTABLE",
    motor_fundamental_hz=None,
    clip_rate_max=0.0,
    imu_contamination_likely=False,
    ekf_impact_likely=False,
    notch_filter_needed=False,
    confidence=0.80,
    summary="Vibration within acceptable limits. No notch filter required.",
)

_ROOT_CAUSE = HypothesisRecord(
    id="rc-001",
    title="GPS Integrity Failure → EKF Divergence",
    description=(
        "GPS satellite count dropped from 18 to 4 during RTL, causing "
        "EKF velocity innovations to exceed the IVR threshold. "
        "A lane switch occurred but the backup lane also diverged."
    ),
    evidence_for=["GPS sat count collapse", "EKF IVR=0.63", "Lane switch at T+110s"],
    evidence_against=[],
    missing_evidence=["Second GPS receiver data"],
    confidence=0.85,
    status="confirmed",
    agent_source="GPSIntegrityAgent",
)

_CRASH = CrashInvestigationResult(
    proximate_cause=(
        "EKF position divergence triggered by GPS integrity failure during RTL"
    ),
    root_causes=[_ROOT_CAUSE],
    contributing_factors=[
        ContributingFactor(
            factor="GPS integrity collapse with no receiver redundancy",
            supporting_evidence=["GPS_SAT_COUNT_DROP", "GPS_FIX_DROP",
                                 "GPS_POSITION_GLITCH", "GPSIntegrityAgent"],
            confidence="HIGH",
        ),
        ContributingFactor(
            factor="Power system stress degrading avionics during GPS failure",
            supporting_evidence=["BAT_LOW_CAPACITY", "BAT_CURRENT_SPIKE",
                                 "PowerSystemAgent"],
            confidence="MEDIUM",
        ),
    ],
    refuted_hypotheses=[],
    missing_evidence=["Second GPS receiver telemetry"],
    causal_chain=(
        "GPS degradation → EKF innovation exceeded → Lane switch → "
        "Position divergence → RTL guidance failure → CRASH"
    ),
    five_why=[
        "Aircraft lost position control during RTL",
        "EKF position estimate diverged from actual position",
        "GPS integrity collapsed (sat count 18→4, HDOP 1.2→4.8)",
        "GPS antenna exposed to vibration from motors",
        "Vibration isolation mounts were inadequate for this frame",
    ],
    overall_confidence=0.85,
    confidence_label="HIGH",
    corrective_actions=[
        CorrectiveAction(
            priority="IMMEDIATE",
            action="Inspect GPS antenna for physical damage and verify mounting",
            parameter="GPS_ANTENNA_MOUNT",
            parameter_value="external_clear",
            rationale="GPS failure was the primary initiating event",
        ),
        CorrectiveAction(
            priority="SHORT_TERM",
            action="Add second GPS receiver for redundancy",
            rationale="Single GPS point of failure contributed to unrecoverable EKF state",
        ),
        CorrectiveAction(
            priority="LONG_TERM",
            action="Install vibration isolation mounts between GPS and airframe",
            rationale="Vibration-induced lane switch preceded final GPS collapse",
        ),
    ],
    open_questions=["Was GPS failure hardware or environmental?"],
)

_REPORT = ForensicReportLLM(
    classification="CRASH",
    confidence_level="HIGH",
    executive_summary=(
        "GPS integrity failure caused EKF position divergence leading to "
        "loss of navigational control during RTL. GPS_SAT_COUNT_DROP at T+107.9s "
        "initiated EKF degradation confirmed by EKF_LANE_SWITCH at T+111.8s."
    ),
    log_metadata={
        "vehicle": "ArduCopter",
        "fw_version": "V4.6.3",
        "duration_s": 66,
        "format": "ArduPilot BIN",
    },
    causal_chain=(
        "GPS_SAT_COUNT_DROP (T+107.9s) → EKF_LANE_SWITCH (T+111.8s) → "
        "Position divergence → RTL guidance failure → CRASH"
    ),
    hypothesis_analysis=[_ROOT_CAUSE],
    root_cause_determination=(
        "EKF position divergence triggered by GPS integrity failure during RTL"
    ),
    root_cause_confidence=0.85,
    # contributing_factors removed from ForensicReportLLM — injected from
    # CrashInvestigatorAgent's grounded output, not regenerated by LLM
    corrective_actions=_CRASH.corrective_actions,
    open_questions=["Was GPS failure hardware or environmental?"],
    raw_evidence_summary={
        "GPS_SAT_COUNT_DROP": "18->0 at T+107.9s",
        "EKF_LANE_SWITCH": "FAILSAFE_EKFINAV at T+111.8s",
        "BAT_LOW_CAPACITY": "critically low from T+71.3s",
    },
)

# Map response_model class name → canned instance
_CANNED: dict[str, object] = {
    "EKFDiagnosticResult": _EKF,
    "GPSIntegrityResult": _GPS,
    "PowerSystemResult": _POWER,
    "VibrationResult": _VIB,
    "CrashInvestigationResult": _CRASH,
    "ForensicReportLLM": _REPORT,
}


# ── stub client ───────────────────────────────────────────────────────────────

def _make_stub_client() -> InferenceClient:
    """
    Return a MagicMock that satisfies the InferenceClient interface.

    Patch target: llm.client._client  (same as before — nothing in agents changes)
    """
    stub = MagicMock(spec=InferenceClient)
    stub.primary_model = "stub-model"
    stub.fast_model = "stub-model"
    stub.embedding_model = "stub-embed"

    def _structured(*args, response_model=None, **kwargs):
        key = response_model.__name__ if response_model else ""
        if key not in _CANNED:
            raise ValueError(f"StubInferenceClient: no canned response for {key!r}")
        return _CANNED[key]

    stub.structured.side_effect = _structured
    stub.fast_structured.side_effect = _structured
    stub.complete.return_value = "Stub completion: investigation summary."
    stub.embed.return_value = [0.0] * 768
    stub.embed_batch.return_value = [[0.0] * 768]
    stub.get_usage_summary.return_value = []

    return stub


# ── pytest fixture ────────────────────────────────────────────────────────────

@pytest.fixture()
def stub_llm():
    """
    Patch llm.client._client with a stub InferenceClient.

    All agents that call get_llm_client() receive the stub, making the full
    orchestrator/agent/storage pipeline run in <30 s without any LLM server.
    """
    stub = _make_stub_client()
    with patch("llm.client._client", stub):
        yield stub
