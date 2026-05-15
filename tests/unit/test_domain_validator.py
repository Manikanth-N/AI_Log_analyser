"""
Unit tests for Phase 5B.1 — Semantic domain admissibility validator.

These tests enforce the core diagnostic safety invariant:
  A contributing factor that claims domain X (GPS, EKF, vibration, etc.)
  must have at least one evidence ID from domain X.

The critical regression case: bat_anomaly_002 pattern —
  "GPS satellite count drop + EKF divergence" claim backed only by BAT_* evidence.
  This MUST be rejected.
"""

from __future__ import annotations

import pytest

from intelligence.domain_validator import check_factor_domain, check_claim_text_domain


# ── check_factor_domain ───────────────────────────────────────────────────────

class TestCheckFactorDomain:

    def test_battery_claim_with_battery_evidence_passes(self):
        ok, _ = check_factor_domain(
            "Battery critically low at 0.4% remaining",
            ["BAT_LOW_CAPACITY", "PowerSystemAgent"],
        )
        assert ok

    def test_gps_claim_with_gps_evidence_passes(self):
        ok, _ = check_factor_domain(
            "GPS satellite count dropped from 18 to 0",
            ["GPS_SAT_COUNT_DROP", "GPSIntegrityAgent"],
        )
        assert ok

    def test_ekf_claim_with_ekf_evidence_passes(self):
        ok, _ = check_factor_domain(
            "EKF divergence causing navigation failure",
            ["EKF_LANE_SWITCH", "EKFDiagnosticsAgent"],
        )
        assert ok

    def test_vibration_claim_with_vibration_evidence_passes(self):
        ok, _ = check_factor_domain(
            "Vibration-induced IMU contamination degrading sensor fusion",
            ["IMU_RAW_EXTREME", "VibrationAnalysisAgent"],
        )
        assert ok

    # ── bat_anomaly_002 regression: GPS claim backed by battery evidence ──────

    def test_gps_claim_with_only_battery_evidence_rejected(self):
        """Core regression: GPS satellite drop claim with BAT_* evidence only → REJECT."""
        ok, reason = check_factor_domain(
            "GPS satellite count drop causing EKF innovation ratio to exceed thresholds",
            ["BAT_LOW_CAPACITY", "BAT_CURRENT_SPIKE", "PowerSystemAgent"],
        )
        assert not ok, "GPS claim with battery-only evidence must be rejected"
        assert "gps" in reason.lower() or "GPS" in reason

    def test_ekf_claim_with_only_battery_evidence_rejected(self):
        """EKF divergence claim with BAT_* evidence only → REJECT."""
        ok, reason = check_factor_domain(
            "EKF innovation ratio exceeding thresholds causing navigation failure",
            ["BAT_LOW_CAPACITY", "PowerSystemAgent"],
        )
        assert not ok
        assert "EKF" in reason or "ekf" in reason.lower()

    def test_gps_claim_with_motor_evidence_rejected(self):
        ok, reason = check_factor_domain(
            "GPS signal degradation due to motor interference",
            ["MOTOR_IMBALANCE", "ESCMotorAgent"],
        )
        assert not ok

    def test_vibration_claim_with_battery_evidence_rejected(self):
        ok, reason = check_factor_domain(
            "Vibration-induced IMU contamination",
            ["BAT_LOW_CAPACITY"],
        )
        assert not ok

    # ── cross-domain claims that have sufficient evidence ─────────────────────

    def test_gps_ekf_claim_with_both_domains_passes(self):
        """Factor claiming GPS caused EKF failure — needs evidence from BOTH."""
        ok, _ = check_factor_domain(
            "GPS integrity collapse causing EKF divergence",
            ["GPS_SAT_COUNT_DROP", "EKF_LANE_SWITCH", "GPSIntegrityAgent"],
        )
        assert ok

    def test_gps_ekf_claim_with_only_gps_passes(self):
        """GPS claim → GPS evidence satisfies GPS domain. EKF keyword without EKF evidence is ok
        because 'ekf' here is the effect, not the system being diagnosed."""
        # "GPS collapse causing EKF failure" — GPS domain is the claimed cause
        # The EKF effect is acceptable context without EKF-specific evidence
        # because the GPS evidence is what grounds the claim.
        # Note: if EKF is in the claim, we do require EKF evidence too.
        ok, reason = check_factor_domain(
            "GPS collapse causing EKF failure",
            ["GPS_SAT_COUNT_DROP"],
        )
        # Both GPS AND EKF keywords detected — both need evidence
        # GPS domain: satisfied by GPS_SAT_COUNT_DROP
        # EKF domain: NOT satisfied — should fail
        assert not ok

    def test_gps_ekf_claim_with_gps_and_ekf_passes(self):
        ok, _ = check_factor_domain(
            "GPS collapse causing EKF failure",
            ["GPS_SAT_COUNT_DROP", "EKF_LANE_SWITCH"],
        )
        assert ok

    # ── edge cases ────────────────────────────────────────────────────────────

    def test_generic_claim_no_domain_keywords_always_passes(self):
        """Factor with no domain keywords — no domain constraint applies."""
        ok, _ = check_factor_domain(
            "Inadequate pre-flight risk assessment",
            ["UNSAFE_ARM"],
        )
        assert ok

    def test_empty_evidence_always_fails(self):
        """Empty evidence fails regardless of claim — stage 1 (existence) catches this
        before domain check, but domain check should also fail gracefully."""
        ok, _ = check_factor_domain(
            "GPS signal loss",
            [],
        )
        assert not ok

    def test_compass_claim_needs_mag_evidence(self):
        ok, reason = check_factor_domain(
            "Compass inconsistency causing heading error",
            ["BAT_LOW_CAPACITY"],
        )
        assert not ok

    def test_motor_claim_with_motor_evidence_passes(self):
        ok, _ = check_factor_domain(
            "Motor output drop causing thrust asymmetry",
            ["MOTOR_OUTPUT_DROP", "ESCMotorAgent"],
        )
        assert ok


# ── check_claim_text_domain ───────────────────────────────────────────────────

class TestCheckClaimTextDomain:
    """Tests for free-text proximate_cause domain validation."""

    def test_battery_claim_with_battery_evidence_no_warnings(self):
        warnings = check_claim_text_domain(
            "Battery exhaustion caused hard landing",
            {"BAT_LOW_CAPACITY", "BAT_CURRENT_SPIKE", "PowerSystemAgent"},
        )
        assert warnings == []

    def test_gps_claim_with_gps_evidence_no_warnings(self):
        warnings = check_claim_text_domain(
            "GPS satellite count dropped to zero triggering EKF failsafe",
            {"GPS_SAT_COUNT_DROP", "EKF_LANE_SWITCH", "GPSIntegrityAgent"},
        )
        assert warnings == []

    def test_gps_claim_with_battery_only_evidence_warns(self):
        """bat_anomaly_002 proximate_cause: GPS/EKF claim with no GPS/EKF evidence."""
        warnings = check_claim_text_domain(
            "GPS satellite count drop and EKF innovation ratio caused failsafe",
            {"BAT_LOW_CAPACITY", "BAT_CURRENT_SPIKE", "PowerSystemAgent"},
        )
        assert len(warnings) >= 1
        # Should mention GPS domain missing
        combined = " ".join(warnings).lower()
        assert "gps" in combined or "GPS" in combined

    def test_no_domain_keywords_no_warnings(self):
        warnings = check_claim_text_domain(
            "The aircraft experienced an uncontrolled descent",
            {"BAT_LOW_CAPACITY"},
        )
        assert warnings == []

    def test_vibration_claim_no_vibe_evidence_warns(self):
        warnings = check_claim_text_domain(
            "Vibration-induced IMU contamination caused EKF degradation",
            {"BAT_LOW_CAPACITY", "EKF_LANE_SWITCH"},
        )
        assert any("vibration" in w.lower() or "VIBE" in w for w in warnings)


# ── integration: validate_contributing_factors with domain check ──────────────

class TestValidateContributingFactorsWithDomain:
    """End-to-end test of the CrashInvestigatorAgent validate_contributing_factors
    function, which now runs both existence and domain checks."""

    def test_bat_anomaly_002_pattern_rejected(self):
        """
        Regression: bat_anomaly_002 produced GPS/EKF contributing factor
        citing only battery evidence. After 5B.1 fix, this must be rejected.
        """
        from llm.structured import ContributingFactor
        from agents.crash_investigator import validate_contributing_factors

        factors = [
            ContributingFactor(
                factor="GPS satellite count drop and EKF innovation ratio exceeding thresholds",
                supporting_evidence=["BAT_LOW_CAPACITY", "BAT_CURRENT_SPIKE", "PowerSystemAgent"],
                confidence="HIGH",
            ),
            ContributingFactor(
                factor="Battery critically low at 0.4% remaining",
                supporting_evidence=["BAT_LOW_CAPACITY", "PowerSystemAgent"],
                confidence="HIGH",
            ),
        ]
        valid_ids = {"BAT_LOW_CAPACITY", "BAT_CURRENT_SPIKE", "PowerSystemAgent"}

        grounded, unsupported = validate_contributing_factors(factors, valid_ids)

        assert len(unsupported) == 1, "GPS/EKF claim with battery-only evidence must be unsupported"
        assert "GPS" in unsupported[0].factor or "satellite" in unsupported[0].factor.lower()
        assert len(grounded) == 1
        assert "Battery" in grounded[0].factor or "battery" in grounded[0].factor.lower()

    def test_gps_crash_006_pattern_passes(self):
        """gps_crash_006 GPS factor with GPS evidence — must be grounded."""
        from llm.structured import ContributingFactor
        from agents.crash_investigator import validate_contributing_factors

        factors = [
            ContributingFactor(
                factor="GPS integrity collapse with no receiver redundancy",
                supporting_evidence=["GPS_SAT_COUNT_DROP", "GPS_FIX_DROP", "GPSIntegrityAgent"],
                confidence="HIGH",
            ),
        ]
        valid_ids = {"GPS_SAT_COUNT_DROP", "GPS_FIX_DROP", "EKF_LANE_SWITCH",
                     "GPSIntegrityAgent", "EKFDiagnosticsAgent"}

        grounded, unsupported = validate_contributing_factors(factors, valid_ids)

        assert len(grounded) == 1
        assert len(unsupported) == 0
