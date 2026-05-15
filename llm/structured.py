"""
Pydantic response models for structured LLM outputs.
These enforce that LLMs return only verifiable, schema-valid data.
The LLM cannot invent fields not in these models.
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


Severity = Literal["INFO", "WARNING", "CRITICAL", "FATAL"]
Confidence = Literal["LOW", "MEDIUM", "HIGH", "DEFINITIVE"]
HypothesisStatus = Literal["forming", "supported", "refuted", "confirmed"]


# ─────────────────────────────────────────────────────────────────────────────
# SHARED PRIMITIVES
# ─────────────────────────────────────────────────────────────────────────────

class TimestampedEvent(BaseModel):
    timestamp_us: int
    description: str
    severity: Severity = "INFO"


class EvidenceItem(BaseModel):
    field: str                # e.g., "NKF4.var_ratio_vel"
    timestamp_us: int
    value: float
    context: str              # what this value means in this context


# ─────────────────────────────────────────────────────────────────────────────
# EKF DIAGNOSTICS
# ─────────────────────────────────────────────────────────────────────────────

class EKFDiagnosticResult(BaseModel):
    filter_health: Literal["HEALTHY", "STRESSED", "DEGRADED", "FAILED"]
    primary_failure_mode: Optional[str] = None
    # e.g., "GPS velocity innovation exceeded IVR threshold"
    innovation_ratio_max: Optional[float] = None
    innovation_ratio_sustained_s: Optional[float] = None
    lane_switch_occurred: bool = False
    lane_switch_timestamp_us: Optional[int] = None
    filter_recovered: bool = False
    position_trustworthy_after_event: bool = True
    magnetic_anomaly: bool = False
    height_estimate_reliable: bool = True
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    evidence: list[EvidenceItem] = Field(default_factory=list)
    causal_sensor: Optional[Literal["GPS", "BARO", "COMPASS", "IMU", "UNKNOWN"]] = None


# ─────────────────────────────────────────────────────────────────────────────
# GPS INTEGRITY
# ─────────────────────────────────────────────────────────────────────────────

class GPSIntegrityResult(BaseModel):
    integrity_score: float = Field(ge=0.0, le=100.0)
    # 0 = no signal, 100 = perfect
    glitches_detected: int = 0
    max_glitch_magnitude_m: Optional[float] = None
    hdop_min: Optional[float] = None
    hdop_max: Optional[float] = None
    hdop_at_failure: Optional[float] = None
    sat_count_min: Optional[int] = None
    sat_count_at_failure: Optional[int] = None
    spoofing_likelihood: Literal["NONE", "LOW", "MEDIUM", "HIGH"] = "NONE"
    spoofing_indicators: list[str] = Field(default_factory=list)
    degradation_start_us: Optional[int] = None
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    causal_to_ekf_failure: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# POWER SYSTEM
# ─────────────────────────────────────────────────────────────────────────────

class PowerSystemResult(BaseModel):
    voltage_at_failure_v: Optional[float] = None
    min_voltage_v: Optional[float] = None
    max_current_a: Optional[float] = None
    estimated_r_internal_ohm: Optional[float] = None
    brownout_detected: bool = False
    brownout_timestamp_us: Optional[int] = None
    current_spikes: int = 0
    battery_soc_at_failure_pct: Optional[float] = None
    failsafe_triggered: bool = False
    failsafe_threshold_correct: Optional[bool] = None
    power_causal_to_failure: bool = False
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str


# ─────────────────────────────────────────────────────────────────────────────
# VIBRATION
# ─────────────────────────────────────────────────────────────────────────────

class VibrationResult(BaseModel):
    rms_x: Optional[float] = None
    rms_y: Optional[float] = None
    rms_z: Optional[float] = None
    overall_severity: Literal["GOOD", "ACCEPTABLE", "WARNING", "CRITICAL"] = "GOOD"
    motor_fundamental_hz: Optional[float] = None
    clip_rate_max: Optional[float] = None
    imu_contamination_likely: bool = False
    ekf_impact_likely: bool = False
    notch_filter_needed: bool = False
    recommended_notch_hz: Optional[float] = None
    unidentified_peaks_hz: list[float] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str


# ─────────────────────────────────────────────────────────────────────────────
# HYPOTHESIS
# ─────────────────────────────────────────────────────────────────────────────

class HypothesisRecord(BaseModel):
    id: str
    title: str
    description: str
    evidence_for: list[str] = Field(default_factory=list)
    evidence_against: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    status: HypothesisStatus
    agent_source: str


# ─────────────────────────────────────────────────────────────────────────────
# CRASH INVESTIGATION / ROOT CAUSE
# ─────────────────────────────────────────────────────────────────────────────

class CorrectiveAction(BaseModel):
    priority: Literal["IMMEDIATE", "SHORT_TERM", "LONG_TERM"]
    action: str
    parameter: Optional[str] = None      # e.g., "INS_NOTCH_FREQ"
    parameter_value: Optional[str] = None
    rationale: str


class ContributingFactor(BaseModel):
    """
    A contributing factor with mandatory evidence citations.

    supporting_evidence must be exact rule_names (e.g., GPS_SAT_COUNT_DROP)
    or agent names (e.g., GPSIntegrityAgent) from the evidence package.
    Factors with no valid supporting_evidence are structurally rejected.
    """
    factor: str                          # human-readable description
    supporting_evidence: list[str]       # exact rule_names or agent names
    confidence: Confidence


class CrashInvestigationResult(BaseModel):
    proximate_cause: str
    root_causes: list[HypothesisRecord]
    contributing_factors: list[ContributingFactor]
    refuted_hypotheses: list[HypothesisRecord]
    missing_evidence: list[str]
    causal_chain: str                    # ASCII diagram
    five_why: list[str]                  # 5-why analysis steps
    overall_confidence: float = Field(ge=0.0, le=1.0)
    confidence_label: Confidence
    corrective_actions: list[CorrectiveAction]
    open_questions: list[str]


# ─────────────────────────────────────────────────────────────────────────────
# FLIGHT PHASE TIMELINE
# ─────────────────────────────────────────────────────────────────────────────

class FlightPhase(BaseModel):
    name: str
    start_us: int
    end_us: int
    mode_name: str
    notes: str = ""

    @property
    def duration_s(self) -> float:
        return (self.end_us - self.start_us) / 1_000_000.0


class TimelineResult(BaseModel):
    phases: list[FlightPhase]
    key_events: list[TimestampedEvent]
    event_horizon_us: Optional[int] = None
    # timestamp of last normal state before failure chain
    crash_detected: bool = False
    crash_timestamp_us: Optional[int] = None
    crash_mode: Optional[str] = None
    arm_timestamp_us: Optional[int] = None
    flight_duration_s: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# FINAL INVESTIGATION REPORT
# ─────────────────────────────────────────────────────────────────────────────

class AnomalyRegistryEntry(BaseModel):
    """One entry in the anomaly registry. Built deterministically from detector output."""
    timestamp_us: int
    subsystem: str        # maps from anomaly["category"]
    severity: Severity
    description: str
    rule_name: str        # exact rule identifier from the rules engine
    raw_values: dict = Field(default_factory=dict)  # actual telemetry metrics


class ForensicReportLLM(BaseModel):
    """
    LLM-generated portion of the forensic report.

    Excludes anomaly_registry and flight_phase_timeline — both are injected
    deterministically from detector/state data after the LLM call.
    The LLM cannot invent evidence registries.
    """
    # Section 1
    classification: Literal["CRASH", "ANOMALY", "REVIEW", "NOMINAL"]
    confidence_level: Confidence
    executive_summary: str

    # Section 2
    log_metadata: dict

    # Section 5 — causal chain narrative (phases and anomaly table are injected)
    causal_chain: str

    # Section 6
    hypothesis_analysis: list[HypothesisRecord]

    # Section 7
    root_cause_determination: str
    root_cause_confidence: float = Field(ge=0.0, le=1.0)

    # Section 8 — contributing_factors are injected from CrashInvestigatorAgent's
    # grounded, validated output; the LLM does NOT regenerate them.
    # This field is intentionally absent from ForensicReportLLM.

    # Section 9
    corrective_actions: list[CorrectiveAction]

    # Section 10
    open_questions: list[str]

    # Section 11
    raw_evidence_summary: dict


class ForensicReport(BaseModel):
    """
    Complete forensic report as stored on disk and returned by the API.
    anomaly_registry and flight_phase_timeline are always deterministic.
    """
    # Section 1
    classification: Literal["CRASH", "ANOMALY", "REVIEW", "NOMINAL"]
    confidence_level: Confidence
    executive_summary: str

    # Section 2
    log_metadata: dict

    # Section 3 — deterministic from state["flight_phases"]
    flight_phase_timeline: list[FlightPhase]

    # Section 4 — deterministic from state["anomalies"]
    anomaly_registry: list[AnomalyRegistryEntry]

    # Section 5
    causal_chain: str

    # Section 6
    hypothesis_analysis: list[HypothesisRecord]

    # Section 7
    root_cause_determination: str
    root_cause_confidence: float = Field(ge=0.0, le=1.0)

    # Section 8
    contributing_factors: list[str]

    # Section 9
    corrective_actions: list[CorrectiveAction]

    # Section 10
    open_questions: list[str]

    # Section 11
    raw_evidence_summary: dict
