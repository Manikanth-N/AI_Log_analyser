"""
Phase 5C — Narrative safety gate unit tests.

Validates:
  - extract_evidence_domains correctly maps anomaly rule_names and agent names to domain labels
  - validate_narrative catches cross-domain keyword leakage in free-text fields
  - apply_narrative_safety_gate replaces violating fields with structured-first fallback
  - clean narratives pass without modification
"""

from __future__ import annotations

import pytest

from intelligence.narrative_validator import (
    DomainViolation,
    NarrativeValidationResult,
    apply_narrative_safety_gate,
    build_safe_narrative_from_structured,
    extract_evidence_domains,
    validate_narrative,
)


# ── evidence domain extraction ────────────────────────────────────────────────

def test_gps_anomaly_activates_gps_domain():
    anomalies = [{"rule_name": "GPS_SAT_COUNT_DROP"}]
    domains = extract_evidence_domains(anomalies, {})
    assert "GPS" in domains


def test_ekf_anomaly_activates_ekf_domain():
    anomalies = [{"rule_name": "EKF_LANE_SWITCH"}]
    domains = extract_evidence_domains(anomalies, {})
    assert "EKF" in domains


def test_power_agent_activates_power_domain():
    domains = extract_evidence_domains([], {"PowerSystemAgent": {"summary": "..."}})
    assert "POWER" in domains


def test_multiple_anomaly_types_activate_correct_domains():
    anomalies = [
        {"rule_name": "GPS_SAT_COUNT_DROP"},
        {"rule_name": "BAT_LOW_CAPACITY"},
        {"rule_name": "EKF_VEL_INNOV_SPIKE"},
    ]
    domains = extract_evidence_domains(anomalies, {})
    assert domains >= {"GPS", "POWER", "EKF"}
    assert "VIBRATION" not in domains
    assert "MOTOR" not in domains


def test_vibration_agent_activates_vibration_domain():
    domains = extract_evidence_domains([], {"VibrationAnalysisAgent": {}})
    assert "VIBRATION" in domains


def test_empty_evidence_gives_empty_domains():
    domains = extract_evidence_domains([], {})
    assert domains == frozenset()


# ── narrative validation ──────────────────────────────────────────────────────

class TestValidateNarrative:

    def test_clean_narrative_passes(self):
        result = validate_narrative(
            narrative_fields={"executive_summary": "Battery capacity was critically low."},
            allowed_domains=frozenset({"POWER"}),
        )
        assert result.valid
        assert result.violations == []

    def test_gps_keyword_in_battery_only_report_is_violation(self):
        result = validate_narrative(
            narrative_fields={
                "executive_summary": "GPS satellite count dropped causing EKF divergence."
            },
            allowed_domains=frozenset({"POWER"}),
        )
        assert not result.valid
        domain_labels = {v.domain for v in result.violations}
        assert "GPS" in domain_labels or "EKF" in domain_labels

    def test_ekf_keyword_with_ekf_evidence_passes(self):
        result = validate_narrative(
            narrative_fields={"causal_chain": "EKF divergence led to navigation failure."},
            allowed_domains=frozenset({"EKF", "GPS"}),
        )
        assert result.valid

    def test_violation_records_field_name(self):
        result = validate_narrative(
            narrative_fields={
                "executive_summary": "Motor failure caused by ESC desync.",
                "causal_chain": "Battery capacity remained normal.",
            },
            allowed_domains=frozenset({"POWER"}),
        )
        assert not result.valid
        violation_fields = {v.field_name for v in result.violations}
        assert "executive_summary" in violation_fields

    def test_multiple_fields_multiple_violations(self):
        result = validate_narrative(
            narrative_fields={
                "executive_summary": "GPS signal degradation caused navigation failure.",
                "causal_chain": "Motor desync combined with EKF divergence.",
            },
            allowed_domains=frozenset({"POWER"}),
        )
        assert not result.valid
        assert len(result.violations) >= 2

    def test_vibration_keyword_blocked_without_vibration_domain(self):
        result = validate_narrative(
            narrative_fields={
                "root_cause_determination": "Vibration-induced IMU contamination was present."
            },
            allowed_domains=frozenset({"GPS", "EKF"}),
        )
        assert not result.valid
        assert any(v.domain == "VIBRATION" for v in result.violations)

    def test_empty_text_fields_pass(self):
        result = validate_narrative(
            narrative_fields={"executive_summary": "", "causal_chain": None},
            allowed_domains=frozenset(),
        )
        assert result.valid

    def test_all_domains_present_passes_any_narrative(self):
        all_domains = frozenset({"GPS", "EKF", "POWER", "VIBRATION", "MOTOR", "COMPASS", "RC"})
        result = validate_narrative(
            narrative_fields={
                "executive_summary": (
                    "GPS satellite loss caused EKF divergence. Motor ESC desync "
                    "combined with vibration-induced IMU contamination and low battery voltage "
                    "contributed. RC failsafe triggered. Compass heading error observed."
                )
            },
            allowed_domains=all_domains,
        )
        assert result.valid

    def test_word_boundary_prevents_partial_match(self):
        # "descent" contains "esc" but should not trigger MOTOR domain
        result = validate_narrative(
            narrative_fields={"executive_summary": "Aircraft entered uncontrolled descent."},
            allowed_domains=frozenset({"EKF", "GPS"}),
        )
        assert result.valid, f"Unexpected violations: {result.violations}"


# ── apply_narrative_safety_gate ───────────────────────────────────────────────

class TestApplyNarrativeSafetyGate:

    def _base_report(self) -> dict:
        return {
            "classification": "ANOMALY",
            "confidence_level": "HIGH",
            "executive_summary": "Battery capacity critically low.",
            "causal_chain": "Battery degradation led to brownout.",
            "root_cause_determination": "Power system failure due to battery capacity loss.",
            "contributing_factors": ["Battery sag [evidence: BAT_LOW_CAPACITY]"],
        }

    def test_clean_report_unchanged(self):
        report = self._base_report()
        anomalies = [{"rule_name": "BAT_LOW_CAPACITY"}]
        result = apply_narrative_safety_gate(report, anomalies, {"PowerSystemAgent": {}})
        assert result["executive_summary"] == "Battery capacity critically low."
        assert result["_narrative_validation"]["valid"] is True

    def test_violated_field_is_replaced(self):
        report = self._base_report()
        # Inject cross-domain claim in executive_summary
        report["executive_summary"] = "GPS satellite failure caused EKF divergence."
        anomalies = [{"rule_name": "BAT_LOW_CAPACITY"}]
        result = apply_narrative_safety_gate(report, anomalies, {"PowerSystemAgent": {}})
        # GPS/EKF keywords must not appear in the replaced summary
        replaced = result["executive_summary"]
        assert "gps" not in replaced.lower() or "GPS" not in replaced
        assert result["_narrative_validation"]["valid"] is False

    def test_validation_metadata_stamped(self):
        report = self._base_report()
        anomalies = [{"rule_name": "BAT_LOW_CAPACITY"}]
        result = apply_narrative_safety_gate(report, anomalies, {})
        assert "_narrative_validation" in result
        assert "valid" in result["_narrative_validation"]
        assert "allowed_domains" in result["_narrative_validation"]

    def test_structured_first_fallback_respects_allowed_domains(self):
        fallback = build_safe_narrative_from_structured(
            proximate_cause="Battery power failure",
            contributing_factors=["Battery sag [evidence: BAT_LOW_CAPACITY]"],
            causal_chain="Battery degradation → brownout",
            allowed_domains=frozenset({"POWER"}),
            classification="ANOMALY",
            confidence="HIGH",
        )
        # Validate the fallback itself
        validation = validate_narrative(
            narrative_fields=fallback,
            allowed_domains=frozenset({"POWER"}),
        )
        assert validation.valid, f"Fallback narrative violated domains: {validation.violations}"


# ── cross-domain hallucination benchmark assertions ───────────────────────────

class TestCrossDomainHallucinationGuard:
    """
    Regression tests for the specific failure modes identified in Phase 5B benchmarks.
    bat_anomaly_002: executive_summary hallucinated GPS/EKF despite zero GPS/EKF evidence.
    """

    def test_bat_anomaly_002_gps_ekf_blocked_from_narrative(self):
        """bat_anomaly_002: battery-only evidence → GPS/EKF narrative rejected."""
        anomalies = [
            {"rule_name": "BAT_LOW_CAPACITY"},
            {"rule_name": "BAT_CURRENT_SPIKE"},
        ]
        agent_findings = {"PowerSystemAgent": {}}
        allowed = extract_evidence_domains(anomalies, agent_findings)

        bad_narrative = (
            "GPS integrity failure caused EKF position divergence "
            "during RTL, leading to navigational control loss."
        )
        result = validate_narrative(
            narrative_fields={"executive_summary": bad_narrative},
            allowed_domains=allowed,
        )
        assert not result.valid
        domains_violated = {v.domain for v in result.violations}
        assert "GPS" in domains_violated or "EKF" in domains_violated

    def test_gps_crash_006_gps_ekf_allowed(self):
        """gps_crash_006: GPS + EKF evidence → GPS/EKF narrative passes."""
        anomalies = [
            {"rule_name": "GPS_SAT_COUNT_DROP"},
            {"rule_name": "GPS_POSITION_GLITCH"},
            {"rule_name": "EKF_LANE_SWITCH"},
        ]
        agent_findings = {
            "GPSIntegrityAgent": {},
            "EKFDiagnosticsAgent": {},
        }
        allowed = extract_evidence_domains(anomalies, agent_findings)

        good_narrative = (
            "GPS satellite count collapsed from 18 to 4, triggering EKF divergence "
            "and lane switch. Position estimate became unreliable."
        )
        result = validate_narrative(
            narrative_fields={"executive_summary": good_narrative},
            allowed_domains=allowed,
        )
        assert result.valid, f"Unexpected violations: {result.violations}"
