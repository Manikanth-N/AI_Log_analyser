"""
Semantic domain admissibility validator for contributing factors and root-cause claims.

Problem: the LLM can produce a grammatically valid contributing factor whose TEXT claims
one subsystem domain (e.g. GPS, EKF) but whose EVIDENCE only covers a different domain
(e.g. battery only). This is the core failure mode of bat_anomaly_002, where the model
cited "GPS satellite count drop + EKF innovation ratio" despite zero GPS/EKF anomalies.

Rule: if a factor's text claims domain X, its supporting_evidence must include at least
one X-domain evidence ID (rule_name or agent name).  Cross-domain narrative is only
allowed when X-domain evidence exists — not when the LLM invents it.

This is applied AFTER the existence check in validate_contributing_factors(), so every
evidence ID is already confirmed as real before domain validation runs.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Domain → (claim keywords, evidence prefixes / exact names)
#
# "claim keywords": if any appear in factor.factor.lower(), the factor claims
#   that domain and must supply matching evidence.
# "evidence prefixes": a supporting_evidence ID satisfies the domain if it
#   starts with any of these strings (or equals any of them exactly).
# ---------------------------------------------------------------------------

_DOMAIN_RULES: list[tuple[frozenset[str], tuple[str, ...]]] = [
    # GPS / GNSS navigation
    (
        frozenset({
            "gps", "gnss", "satellite", "hdop", "vdop",
            "sat count", "sacc", "pacc", "gps signal",
        }),
        ("GPS_", "GPSIntegrityAgent"),
    ),
    # EKF / state estimation / position estimation
    (
        frozenset({
            "ekf", "kalman", "innovation ratio", "ivr", "lane switch",
            "ekf divergence", "position estimate", "navigation filter",
            "ekfinav", "ekf lane",
        }),
        ("EKF_", "EKFDiagnosticsAgent"),
    ),
    # Vibration / IMU contamination
    (
        frozenset({
            "vibration", "vibe", "imu contamination", "imu clipping",
            "mechanical resonance", "prop wash", "motor resonance",
        }),
        ("VIBE_", "IMU_", "VibrationAnalysisAgent"),
    ),
    # Compass / magnetic / heading
    (
        frozenset({
            "compass", "magnetic", "magnetometer", "heading error",
            "mag inconsistency", "magnetic interference",
        }),
        ("EKF_MAG_", "COMPASS_", "MAG_", "EKF_MAG_INNOV"),
    ),
    # Motor / ESC / thrust
    (
        frozenset({
            "motor", "esc", "desync", "thrust asymmetry", "motor failure",
            "motor imbalance", "motor output", "motor jam",
        }),
        ("MOTOR_", "ESC_", "ESCMotorAgent"),
    ),
    # RC / radio link / failsafe
    (
        frozenset({
            "rc failsafe", "radio link", "rc link loss",
            "rc signal", "telemetry loss",
        }),
        ("RC_", "FAILSAFE_VERIFY", "MissionBehaviorAgent"),
    ),
]


def _evidence_matches_domain(
    evidence_ids: list[str],
    required_prefixes: tuple[str, ...],
) -> bool:
    """Return True if at least one evidence ID starts with (or equals) a required prefix."""
    for ev in evidence_ids:
        for prefix in required_prefixes:
            if ev == prefix or ev.startswith(prefix):
                return True
    return False


def _keyword_in_text(keyword: str, text: str) -> bool:
    """
    Match keyword against lowercased text with word-boundary semantics for
    single-word keywords.  Multi-word keywords use plain substring so that
    phrases like "gps signal" still match across word boundaries naturally.

    Word-boundary matching prevents 'esc' from matching 'descent', etc.
    """
    if " " in keyword:
        return keyword in text
    return bool(re.search(r"\b" + re.escape(keyword) + r"\b", text))


def _claimed_domains(factor_text: str) -> list[tuple[str, tuple[str, ...]]]:
    """
    Return all (domain_label, required_prefixes) pairs whose keywords appear
    in the factor text.
    """
    text = factor_text.lower()
    claimed = []
    for keywords, prefixes in _DOMAIN_RULES:
        if any(_keyword_in_text(kw, text) for kw in keywords):
            domain_label = "/".join(sorted(prefixes)[:2])
            claimed.append((domain_label, prefixes))
    return claimed


def check_factor_domain(
    factor_text: str,
    supporting_evidence: list[str],
) -> tuple[bool, str]:
    """
    Verify that every domain claimed in factor_text has matching evidence.

    Returns:
        (admissible, reason)
        admissible=True  → factor is semantically grounded
        admissible=False → factor claims a domain with no supporting evidence
    """
    claimed = _claimed_domains(factor_text)
    if not claimed:
        # No specific domain detected — no domain constraint applies
        return True, "no domain keywords detected"

    violations: list[str] = []
    for domain_label, required_prefixes in claimed:
        if not _evidence_matches_domain(supporting_evidence, required_prefixes):
            cited = ", ".join(supporting_evidence[:3]) or "(none)"
            violations.append(
                f"claims {domain_label!r} domain but evidence [{cited}] "
                f"has no {domain_label}-domain IDs"
            )

    if violations:
        return False, "; ".join(violations)
    return True, "ok"


def check_claim_text_domain(
    claim_text: str,
    valid_evidence_ids: set[str],
) -> list[str]:
    """
    Check a free-text claim (e.g. proximate_cause) for domain keywords that have
    no matching evidence in valid_evidence_ids.

    Returns list of warning strings (empty = no domain violations detected).
    """
    text = claim_text.lower()
    warnings: list[str] = []
    for keywords, required_prefixes in _DOMAIN_RULES:
        if any(_keyword_in_text(kw, text) for kw in keywords):
            has_evidence = any(
                any(ev == p or ev.startswith(p) for p in required_prefixes)
                for ev in valid_evidence_ids
            )
            if not has_evidence:
                domain_label = "/".join(sorted(required_prefixes)[:2])
                warnings.append(
                    f"claim mentions {domain_label!r} domain but no "
                    f"{domain_label}-domain evidence exists in this flight"
                )
    return warnings
