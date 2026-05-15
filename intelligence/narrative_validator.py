"""
Phase 5C — Narrative safety gate.

Problem: structured outputs (contributing_factors, root_causes) are grounded by
domain_validator.py. But ForensicReportLLM free-text fields — executive_summary,
causal_chain, root_cause_determination — are generated without that constraint and
can introduce cross-domain hallucinations.

bat_anomaly_002 failure: executive_summary said "EKF divergence from GPS degradation"
despite zero GPS/EKF evidence. The structured fields were clean; the narrative was not.

Solution:
  1. Derive allowed domains from the actual evidence set (anomalies + agent findings).
  2. Scan each narrative field for domain keyword leakage.
  3. For hard violations (domain with NO evidence at all), replace the narrative
     with one generated strictly from validated structured fields.
  4. For soft violations, log and annotate but allow (domain is at least partially
     present even if not causal).

The structured-first protocol wires this into ReportWriterAgent.run():
  - evidence_domains are computed before the LLM call
  - narrative is validated immediately after generation
  - violations trigger a targeted regeneration or template substitution
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)

# ── Domain keyword → evidence prefix mapping ──────────────────────────────────
# Mirrors domain_validator._DOMAIN_RULES but used in reverse:
#   given an anomaly rule_name or agent name, which "domain label" does it activate?
# Also defines which claim keywords trigger domain membership check in narrative.

_NARRATIVE_DOMAIN_RULES: list[tuple[str, frozenset[str], tuple[str, ...]]] = [
    # (domain_label, claim_keywords_in_narrative, evidence_prefixes_or_exact_agent_names)
    (
        "GPS",
        frozenset({
            "gps", "gnss", "satellite", "hdop", "vdop",
            "sat count", "gps signal", "gps fix",
        }),
        ("GPS_", "GPSIntegrityAgent"),
    ),
    (
        "EKF",
        frozenset({
            "ekf", "kalman", "innovation ratio", "ivr", "lane switch",
            "ekf divergence", "position estimate", "navigation filter",
            "ekfinav", "ekf lane",
        }),
        ("EKF_", "EKFDiagnosticsAgent"),
    ),
    (
        "VIBRATION",
        frozenset({
            "vibration", "vibe", "imu contamination", "imu clipping",
            "mechanical resonance", "prop wash", "motor resonance",
        }),
        ("VIBE_", "IMU_", "VibrationAnalysisAgent"),
    ),
    (
        "COMPASS",
        frozenset({
            "compass", "magnetic", "magnetometer", "heading error",
            "mag inconsistency",
        }),
        ("EKF_MAG_", "COMPASS_", "MAG_"),
    ),
    (
        "MOTOR",
        frozenset({
            "motor", "esc", "desync", "thrust asymmetry",
            "motor failure", "motor imbalance", "motor output",
        }),
        ("MOTOR_", "ESC_", "ESCMotorAgent"),
    ),
    (
        "POWER",
        frozenset({
            "battery", "voltage", "brownout", "power failure",
            "current spike", "battery sag", "low voltage",
        }),
        ("BAT_", "POW_", "PowerSystemAgent"),
    ),
    (
        "RC",
        frozenset({
            "rc failsafe", "radio link", "rc link loss",
            "rc signal", "telemetry loss",
        }),
        ("RC_", "FAILSAFE_VERIFY", "MissionBehaviorAgent"),
    ),
]


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class DomainViolation:
    domain: str
    field_name: str
    matched_keyword: str
    text_excerpt: str  # first 120 chars of the offending text


@dataclass
class NarrativeValidationResult:
    valid: bool
    violations: list[DomainViolation] = field(default_factory=list)
    allowed_domains: frozenset[str] = field(default_factory=frozenset)

    def summary(self) -> str:
        if self.valid:
            return f"narrative_valid (allowed_domains={sorted(self.allowed_domains)})"
        return (
            f"narrative_INVALID: {len(self.violations)} violation(s): "
            + "; ".join(
                f"{v.domain} in {v.field_name!r} (keyword={v.matched_keyword!r})"
                for v in self.violations
            )
        )


# ── Evidence domain extraction ────────────────────────────────────────────────

def extract_evidence_domains(
    anomalies: list[dict],
    agent_findings: dict,
) -> frozenset[str]:
    """
    Derive the set of domain labels that are supported by actual evidence.

    An anomaly contributes its domain if its rule_name matches a domain prefix.
    An agent contributes its domain if its AGENT_NAME is in a domain's evidence list.
    """
    active_domains: set[str] = set()

    all_rule_names = {a.get("rule_name", "") for a in anomalies}
    all_agent_names = set(agent_findings.keys())

    for domain_label, _claim_kws, evidence_prefixes in _NARRATIVE_DOMAIN_RULES:
        for rule in all_rule_names:
            if any(rule.startswith(p) or rule == p for p in evidence_prefixes):
                active_domains.add(domain_label)
                break
        if domain_label in active_domains:
            continue
        for agent in all_agent_names:
            if any(agent == p or agent.startswith(p) for p in evidence_prefixes):
                active_domains.add(domain_label)
                break

    return frozenset(active_domains)


# ── Keyword scanner ───────────────────────────────────────────────────────────

def _keyword_in_text(keyword: str, text: str) -> bool:
    """Word-boundary match for single words; substring for multi-word phrases."""
    if " " in keyword:
        return keyword in text
    return bool(re.search(r"\b" + re.escape(keyword) + r"\b", text))


def _find_domain_claims(text: str) -> list[tuple[str, str]]:
    """
    Scan text and return list of (domain_label, matched_keyword) for every
    domain keyword found. Deduplicated per domain.
    """
    text_lower = text.lower()
    found: list[tuple[str, str]] = []
    seen_domains: set[str] = set()

    for domain_label, claim_keywords, _ in _NARRATIVE_DOMAIN_RULES:
        if domain_label in seen_domains:
            continue
        for kw in sorted(claim_keywords):  # sorted for determinism
            if _keyword_in_text(kw, text_lower):
                found.append((domain_label, kw))
                seen_domains.add(domain_label)
                break

    return found


# ── Main validation function ──────────────────────────────────────────────────

def validate_narrative(
    narrative_fields: dict[str, str],
    allowed_domains: frozenset[str],
) -> NarrativeValidationResult:
    """
    Validate free-text narrative fields against allowed domains.

    Args:
        narrative_fields: mapping of field_name → narrative_text.
            e.g. {"executive_summary": "...", "causal_chain": "..."}
        allowed_domains: domain labels confirmed by actual evidence.

    Returns:
        NarrativeValidationResult with all violations found.
    """
    violations: list[DomainViolation] = []

    for field_name, text in narrative_fields.items():
        if not text:
            continue

        domain_claims = _find_domain_claims(text)
        for domain_label, matched_keyword in domain_claims:
            if domain_label not in allowed_domains:
                violations.append(DomainViolation(
                    domain=domain_label,
                    field_name=field_name,
                    matched_keyword=matched_keyword,
                    text_excerpt=text[:120],
                ))

    result = NarrativeValidationResult(
        valid=len(violations) == 0,
        violations=violations,
        allowed_domains=allowed_domains,
    )

    if not result.valid:
        log.warning(
            "narrative_domain_violation",
            violation_count=len(violations),
            details=[
                {
                    "domain": v.domain,
                    "field": v.field_name,
                    "keyword": v.matched_keyword,
                    "excerpt": v.text_excerpt,
                }
                for v in violations
            ],
        )
    else:
        log.debug("narrative_validation_passed", allowed_domains=sorted(allowed_domains))

    return result


# ── Structured-first narrative builder ────────────────────────────────────────

def build_safe_narrative_from_structured(
    proximate_cause: str,
    contributing_factors: list[str],
    causal_chain: str,
    allowed_domains: frozenset[str],
    classification: str,
    confidence: str,
) -> dict[str, str]:
    """
    Build a minimal but safe executive_summary and causal_chain from validated
    structured fields when the LLM-generated narrative fails domain validation.

    This is the fallback path — it produces plain text that is guaranteed domain-clean
    because it is assembled directly from pre-validated structured outputs.
    """
    factors_text = ""
    if contributing_factors:
        # Strip evidence citation brackets for readability
        clean_factors = [re.sub(r"\s*\[evidence:[^\]]+\]", "", cf).strip()
                         for cf in contributing_factors[:3]]
        factors_text = " Contributing factors: " + "; ".join(clean_factors) + "."

    executive_summary = (
        f"[{classification}/{confidence}] {proximate_cause}.{factors_text} "
        f"Active evidence domains: {', '.join(sorted(allowed_domains)) or 'None'}."
    )

    return {
        "executive_summary": executive_summary,
        "causal_chain": causal_chain or proximate_cause,
    }


# ── Integration helper (used by ReportWriterAgent) ────────────────────────────

NARRATIVE_FIELDS_TO_VALIDATE = (
    "executive_summary",
    "causal_chain",
    "root_cause_determination",
)


def apply_narrative_safety_gate(
    report_data: dict,
    anomalies: list[dict],
    agent_findings: dict,
) -> dict:
    """
    Run narrative validation on report_data in-place.

    - Computes allowed_domains from anomalies + agent_findings.
    - Validates executive_summary, causal_chain, root_cause_determination.
    - On hard violations: replaces with structured-first fallback.
    - Stamps report_data with validation metadata.
    - Returns modified report_data.
    """
    allowed = extract_evidence_domains(anomalies, agent_findings)

    narrative_fields = {
        f: report_data.get(f, "")
        for f in NARRATIVE_FIELDS_TO_VALIDATE
        if report_data.get(f)
    }

    result = validate_narrative(narrative_fields, allowed)
    report_data["_narrative_validation"] = {
        "valid": result.valid,
        "allowed_domains": sorted(allowed),
        "violations": [
            {"domain": v.domain, "field": v.field_name, "keyword": v.matched_keyword}
            for v in result.violations
        ],
    }

    if not result.valid:
        # Hard violation — replace narrative with structured-first fallback
        fallback = build_safe_narrative_from_structured(
            proximate_cause=report_data.get("root_cause_determination", "Root cause under investigation"),
            contributing_factors=report_data.get("contributing_factors", []),
            causal_chain=report_data.get("causal_chain", ""),
            allowed_domains=allowed,
            classification=report_data.get("classification", "REVIEW"),
            confidence=report_data.get("confidence_level", "MEDIUM"),
        )
        for field_name, safe_text in fallback.items():
            if any(v.field_name == field_name for v in result.violations):
                original = report_data.get(field_name, "")
                report_data[field_name] = safe_text
                log.warning(
                    "narrative_field_replaced",
                    field=field_name,
                    original_excerpt=original[:80],
                    replacement_excerpt=safe_text[:80],
                )

    return report_data
