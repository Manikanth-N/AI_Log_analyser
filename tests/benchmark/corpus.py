"""
Benchmark corpus — ground truth specifications for diagnostic accuracy evaluation.

Each BenchmarkCase defines what an honest, correct investigation of a specific
flight log MUST and MUST NOT conclude. Cases are derived from manual inspection
of raw telemetry + anomaly profiles, not from running the investigation pipeline
(that would be circular).

## Adding a new case

1. Parse the log: parse_log_task.apply(args=[flight_id, path])
2. Inspect anomaly profile: store.read_derived(fid, "anomalies_fast")
3. Inspect timeline: store.read_derived(fid, "timeline")
4. Define expected outcomes based on telemetry evidence
5. Add BenchmarkCase to CORPUS

## Ground truth policy

- required_rule_names: anomaly rule_names that MUST appear in anomaly_registry
  (these are deterministic detector outputs, not LLM outputs)
- required_contributing_evidence: at least one of these must appear in
  contributing_factors[*].supporting_evidence across all grounded factors
- forbidden_contributing_terms: strings that MUST NOT appear in contributing_factors
  unless backed by specific evidence (checked case-insensitively)
- acceptable_root_cause_keywords: at least one must appear in root_cause_determination
  (case-insensitive)

## Confidence level ordering for min_confidence_level

DEFINITIVE > HIGH > MEDIUM > LOW
"""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Classification = Literal["CRASH", "ANOMALY", "REVIEW", "NOMINAL"]
Confidence = Literal["LOW", "MEDIUM", "HIGH", "DEFINITIVE"]

_CONF_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "DEFINITIVE": 3}


@dataclass(frozen=True)
class BenchmarkCase:
    # Identity
    log_filename: str
    case_id: str          # short slug used in test IDs
    description: str

    # Classification expectations
    expected_classification: Classification
    min_confidence_level: Confidence = "MEDIUM"

    # Deterministic anomaly registry expectations
    # These are rule_names from the rules engine — must appear in report.anomaly_registry
    required_rule_names: list[str] = field(default_factory=list)

    # Causal chain expectations
    # At least one of these keywords must appear in root_cause_determination (case-insensitive)
    acceptable_root_cause_keywords: list[str] = field(default_factory=list)

    # Contributing factor grounding
    # At least one grounded factor must cite at least one of these evidence IDs
    required_contributing_evidence: list[str] = field(default_factory=list)

    # Anti-hallucination rules
    # These strings must NOT appear in contributing_factors (case-insensitive)
    # unless explicitly present in supporting_evidence IDs
    forbidden_contributing_terms: list[str] = field(default_factory=list)

    # Metadata
    known_parsed_flight_id: str | None = None   # pre-parsed flight_id if available
    status: Literal["active", "needs_parse", "needs_label", "needs_log"] = "active"
    notes: str = ""

    def confidence_at_least(self, actual: str) -> bool:
        return _CONF_ORDER.get(actual, -1) >= _CONF_ORDER.get(self.min_confidence_level, 0)


# ── Ground truth corpus ───────────────────────────────────────────────────────

CORPUS: list[BenchmarkCase] = [

    # ── 00000006.BIN — GPS integrity failure → EKF divergence → crash ─────────
    BenchmarkCase(
        log_filename="00000006.BIN",
        case_id="gps_crash_006",
        description="GPS satellite count collapse (18→0) at T+107.9s triggers EKF lane switch and crash during RTL",
        expected_classification="CRASH",
        min_confidence_level="HIGH",
        required_rule_names=[
            "GPS_SAT_COUNT_DROP",
            "EKF_LANE_SWITCH",
        ],
        acceptable_root_cause_keywords=[
            "gps", "ekf", "satellite", "navigation", "position",
        ],
        required_contributing_evidence=[
            "GPS_SAT_COUNT_DROP",
            "GPS_FIX_DROP",
            "GPS_POSITION_GLITCH",
            "GPSIntegrityAgent",
            "EKFDiagnosticsAgent",
        ],
        forbidden_contributing_terms=[
            "urban canyon",
            "multipath",
            "wind shear",
            "pilot error",
            "spoofing",
            "jamming",
        ],
        known_parsed_flight_id="686998f2-95c3-4ef1-b8f9-e7b38ccccdb5",
        notes=(
            "Known crash. GPS_SAT_COUNT_DROP at T+107.9s, FAILSAFE_EKFINAV at T+111.8s "
            "(no actual XKF4 lane change; EKF ran in dead-reckoning after GPS loss). "
            "MAG1 froze at T+104.0s, MAG2 showed 110° heading anomaly at T+104.5s — COMPASS "
            "error precedes GPS loss by 3.9s (possible common EM cause). EK3_CHECK_SCALE=100.0 "
            "(PARAM_OUT_OF_RANGE) prevented normal lane switching. "
            "BAT_LOW_CAPACITY present but voltage stable (24.3V, no brownout) — power was not "
            "causal. Vibration ACCEPTABLE range (mean ~9 m/s², failure-window peak 33.6 m/s²); "
            "do NOT cite as crash cause but do NOT claim 0.0 m/s². "
            "Only LOITER mode in MODE log — 'during RTL' claim is unsupported by telemetry."
        ),
    ),

    # ── 00000002.BIN — Battery depletion anomaly ──────────────────────────────
    BenchmarkCase(
        log_filename="00000002.BIN",
        case_id="bat_anomaly_002",
        description="Battery critically low (0.4% remaining) with current spikes; no GPS/EKF faults; no crash detected",
        expected_classification="ANOMALY",
        min_confidence_level="MEDIUM",
        required_rule_names=[
            "BAT_LOW_CAPACITY",
        ],
        acceptable_root_cause_keywords=[
            "battery", "power", "voltage", "capacity", "current",
            "energy", "depletion", "brownout", "electrical", "charge", "drain",
        ],
        forbidden_contributing_terms=[
            "gps failure", "ekf divergence", "urban canyon",
            "vibration", "collision", "spoofing",
        ],
        known_parsed_flight_id="0d7e848e-76b3-4e93-8781-a71fd09a5fa9",
        status="active",
        notes=(
            "Pure battery case: BAT_LOW_CAPACITY x2 (CRITICAL), BAT_CURRENT_SPIKE x3, "
            "BAT_VOLTAGE_SAG x1. No GPS/EKF/IMU anomalies. No crash horizon detected. "
            "BAT_VOLTAGE_SAG is WARNING (not CRITICAL) — do not require it. "
            "LLM may use 'brownout', 'energy depletion', 'electrical system' — all acceptable."
        ),
    ),

    # ── 00000003.BIN — Battery depletion anomaly (minimal) ───────────────────
    BenchmarkCase(
        log_filename="00000003.BIN",
        case_id="bat_anomaly_003",
        description="Battery critically low with voltage sag; minimal anomaly profile; no crash detected",
        expected_classification="ANOMALY",
        min_confidence_level="MEDIUM",
        required_rule_names=[
            "BAT_LOW_CAPACITY",
        ],
        acceptable_root_cause_keywords=[
            "battery", "power", "voltage", "capacity",
            "energy", "depletion", "brownout", "electrical", "charge",
        ],
        forbidden_contributing_terms=[
            "gps failure", "ekf", "vibration", "urban canyon", "spoofing",
        ],
        known_parsed_flight_id="0e8ce7ca-2a60-4c0e-ba87-3e41c718df70",
        status="active",
        notes=(
            "Minimal profile: BAT_LOW_CAPACITY x2 (CRITICAL), BAT_VOLTAGE_SAG x1 (WARNING). "
            "No GPS/EKF/IMU anomalies. No crash horizon detected."
        ),
    ),

    # ── 00000004.BIN — Battery depletion anomaly ──────────────────────────────
    BenchmarkCase(
        log_filename="00000004.BIN",
        case_id="bat_anomaly_004",
        description="Battery critically low with current spike; no GPS/EKF faults; no crash detected",
        expected_classification="ANOMALY",
        min_confidence_level="MEDIUM",
        required_rule_names=[
            "BAT_LOW_CAPACITY",
        ],
        acceptable_root_cause_keywords=[
            "battery", "power", "voltage", "capacity", "current",
            "energy", "depletion", "brownout", "electrical", "charge",
        ],
        forbidden_contributing_terms=[
            "gps failure", "ekf", "vibration", "urban canyon", "spoofing",
        ],
        known_parsed_flight_id="ac5bc6ae-f2b0-48fe-8280-48e7ab88e65b",
        status="active",
        notes=(
            "BAT_LOW_CAPACITY x2 (CRITICAL), BAT_CURRENT_SPIKE x1 (WARNING). "
            "No GPS/EKF/IMU anomalies. No crash horizon detected."
        ),
    ),

    # ── 00000005.BIN — battery depletion → hard landing / ground impact ──────
    # Forensic verdict: battery at 0.4% (T+264s) → current spike T+328s
    # (possible motor jam during descent) → violent ground impact T+334s
    # (37.5 m/s² excess, 235 IMU events over 0.7s) → EKF velocity spikes from
    # impact bounce. Crash detector did not fire (threshold not met), but
    # impact severity (3.8g) and sequence clearly constitute a crash.
    BenchmarkCase(
        log_filename="00000005.BIN",
        case_id="bat_crash_005",
        description=(
            "Battery critically low (0.4%) → descent → violent ground impact "
            "(IMU 37.5 m/s² excess, 235 events at T+334s) → EKF velocity spikes. "
            "Battery-induced crash, not GPS/navigation failure."
        ),
        expected_classification="CRASH",
        min_confidence_level="MEDIUM",
        required_rule_names=[
            "BAT_LOW_CAPACITY",
            "IMU_RAW_EXTREME",
        ],
        acceptable_root_cause_keywords=[
            "battery", "power", "capacity", "impact", "landing",
            "ground", "failsafe", "depletion", "energy", "brownout",
            "electrical", "crash", "imu", "shock", "charge",
        ],
        required_contributing_evidence=[
            "BAT_LOW_CAPACITY",
            "IMU_RAW_EXTREME",
            "EKF_VEL_INNOV_SPIKE",
        ],
        forbidden_contributing_terms=[
            "gps signal loss", "gps failure", "satellite",
            "urban canyon", "spoofing", "jamming",
            "compass", "magnetic",
        ],
        known_parsed_flight_id="f7bcbcde-73c2-4c04-a0f2-eaaec480a0fb",
        status="active",
        notes=(
            "363 anomalies: BAT_LOW_CAPACITY x2 (CRITICAL, T+264.1s, 0.4% remaining), "
            "BAT_CURRENT_SPIKE x1 (WARNING, T+328.0s, 5.5A vs 1.8A baseline), "
            "IMU_RAW_EXTREME x235 (WARNING, T+334.0-334.7s, 37.5 m/s² excess), "
            "EKF_VEL_INNOV_SPIKE x125 (CRITICAL, T+334.3-337.0s, 3.79 m/s). "
            "No crash horizon from crash detector — threshold not triggered. "
            "Ground truth determined by manual inspection: battery exhaustion → hard landing crash."
        ),
    ),

    # ══════════════════════════════════════════════════════════════════════════
    # CORPUS EXPANSION — additional failure modes (logs not yet available)
    # Status: "needs_log" — activate once a suitable .BIN is sourced and parsed.
    #
    # To add a new case:
    #   1. Copy an appropriate .BIN into logs/
    #   2. Run: parse_log_task.apply(args=[flight_id, path])
    #   3. Inspect: store.read_derived(fid, "anomalies_fast")
    #   4. Fill in required_rule_names, keywords, forbidden_terms
    #   5. Set status="active"
    # ══════════════════════════════════════════════════════════════════════════

    BenchmarkCase(
        log_filename="NOMINAL_FLIGHT.BIN",
        case_id="nominal_flight",
        description="Healthy nominal flight — no anomalies, no crash",
        expected_classification="NOMINAL",
        min_confidence_level="MEDIUM",
        required_rule_names=[],
        acceptable_root_cause_keywords=["nominal", "no fault", "no anomaly", "normal"],
        forbidden_contributing_terms=["crash", "gps failure", "ekf", "battery failure"],
        status="needs_log",
        notes=(
            "Need a clean .BIN log with zero anomalies to test the NOMINAL path. "
            "Key validation: system must not hallucinate faults when none exist."
        ),
    ),

    BenchmarkCase(
        log_filename="RC_FAILSAFE.BIN",
        case_id="rc_failsafe",
        description="RC link loss triggering failsafe → RTL or land",
        expected_classification="ANOMALY",
        min_confidence_level="MEDIUM",
        required_rule_names=["RC_FAILSAFE"],
        acceptable_root_cause_keywords=["rc", "radio", "signal", "failsafe", "link loss"],
        forbidden_contributing_terms=["gps failure", "ekf divergence", "vibration"],
        status="needs_log",
        notes="Need a .BIN with RC_FAILSAFE rule triggered. Check FS_THR_ENABLE=1.",
    ),

    BenchmarkCase(
        log_filename="COMPASS_FAULT.BIN",
        case_id="compass_fault",
        description="Compass inconsistency / magnetic interference causing navigation fault",
        expected_classification="ANOMALY",
        min_confidence_level="MEDIUM",
        required_rule_names=["COMPASS_INCONSISTENCY"],
        acceptable_root_cause_keywords=["compass", "magnetic", "heading", "yaw", "interference"],
        forbidden_contributing_terms=["gps satellite", "rc link loss", "vibration"],
        status="needs_log",
        notes="Need .BIN with COMPASS_INCONSISTENCY. Check for magnetic interference.",
    ),

    BenchmarkCase(
        log_filename="VIBRATION.BIN",
        case_id="vibration_issue",
        description="Chronic vibration causing IMU contamination and EKF degradation",
        expected_classification="ANOMALY",
        min_confidence_level="MEDIUM",
        required_rule_names=["IMU_RAW_EXTREME"],
        acceptable_root_cause_keywords=[
            "vibration", "imu", "mechanical", "resonance", "motor", "propeller",
        ],
        forbidden_contributing_terms=["gps failure", "rc link loss", "compass"],
        status="needs_log",
        notes=(
            "Need .BIN with chronic IMU_RAW_EXTREME (sustained over flight, not just at landing). "
            "This distinguishes vibration issue from bat_crash_005 (impact at end only)."
        ),
    ),

    BenchmarkCase(
        log_filename="MOTOR_FAILURE.BIN",
        case_id="motor_failure",
        description="Motor desync / thrust asymmetry causing loss of control",
        expected_classification="CRASH",
        min_confidence_level="MEDIUM",
        required_rule_names=["MOTOR_IMBALANCE"],
        acceptable_root_cause_keywords=[
            "motor", "thrust", "desync", "esc", "imbalance", "propeller",
        ],
        forbidden_contributing_terms=["gps failure", "rc link loss", "compass"],
        status="needs_log",
        notes="Need .BIN with MOTOR_IMBALANCE or ESC desync rule. ArduCopter 4.x.",
    ),

    BenchmarkCase(
        log_filename="BAT_FAILSAFE.BIN",
        case_id="battery_failsafe",
        description="Battery failsafe triggered — voltage drops below FS threshold → RTL",
        expected_classification="ANOMALY",
        min_confidence_level="MEDIUM",
        required_rule_names=["BAT_LOW_CAPACITY", "BAT_VOLTAGE_SAG"],
        acceptable_root_cause_keywords=[
            "battery", "voltage", "failsafe", "capacity", "power",
        ],
        forbidden_contributing_terms=["gps failure", "ekf", "compass", "vibration"],
        status="needs_log",
        notes=(
            "Distinct from bat_crash_005: aircraft safely RTL'd or landed after failsafe. "
            "No crash/impact. ANOMALY not CRASH."
        ),
    ),

    BenchmarkCase(
        log_filename="GPS_DEGRADATION.BIN",
        case_id="gps_degradation",
        description="GPS quality degrades (HDOP rise, sat count drop) but flight recovers — no crash",
        expected_classification="ANOMALY",
        min_confidence_level="MEDIUM",
        required_rule_names=["GPS_SAT_COUNT_DROP"],
        acceptable_root_cause_keywords=["gps", "satellite", "hdop", "signal", "positioning"],
        forbidden_contributing_terms=["crash", "ekf lane switch", "urban canyon", "spoofing"],
        status="needs_log",
        notes=(
            "Distinct from gps_crash_006: GPS degrades but EKF holds and flight continues. "
            "ANOMALY not CRASH. Tests the false-positive threshold for CRASH classification."
        ),
    ),

    BenchmarkCase(
        log_filename="EKF_DIVERGENCE.BIN",
        case_id="ekf_divergence",
        description="EKF divergence as standalone failure (not GPS-caused) — position loss",
        expected_classification="CRASH",
        min_confidence_level="MEDIUM",
        required_rule_names=["EKF_LANE_SWITCH", "EKF_VEL_INNOV_SPIKE"],
        acceptable_root_cause_keywords=["ekf", "navigation", "position", "divergence"],
        forbidden_contributing_terms=["gps satellite count", "battery", "vibration"],
        status="needs_log",
        notes=(
            "Need a case where EKF diverges without GPS failure as initiator. "
            "Compass or IMU contamination could be the upstream cause."
        ),
    ),
]

# Convenience lookup
CORPUS_BY_CASE_ID = {c.case_id: c for c in CORPUS}
ACTIVE_CORPUS = [c for c in CORPUS if c.status == "active"]
NEEDS_LOG_CORPUS = [c for c in CORPUS if c.status == "needs_log"]
