"""
[CRASH] Crash Sequence Investigator & Root Cause Analyst.
Primary synthesis agent — receives all domain agent findings and produces RCA.

Causal grounding contract:
  - CrashInvestigationResult.contributing_factors is list[ContributingFactor]
  - Each ContributingFactor.supporting_evidence must reference a known
    anomaly rule_name or agent name from the evidence package
  - validate_contributing_factors() rejects factors with no valid evidence
    AND rejects factors whose claim domain does not match their evidence domain
    (e.g. GPS claim with only battery evidence is rejected)
  - Unsupported/cross-domain factors are moved to open_questions, not silently included
"""

import uuid

from intelligence.domain_validator import check_factor_domain, check_claim_text_domain
from llm.prompts.system_prompts import CRASH_INVESTIGATOR_PROMPT
from llm.structured import CrashInvestigationResult, ContributingFactor
from .base import BaseAgent

# Agents that produce domain findings (evidence namespace)
_DOMAIN_AGENT_NAMES = {
    "EKFDiagnosticsAgent",
    "GPSIntegrityAgent",
    "PowerSystemAgent",
    "VibrationAnalysisAgent",
    "ESCMotorAgent",
    "MissionBehaviorAgent",
    "FlightDynamicsAgent",
    "ParameterDriftAgent",
    "SafetyComplianceAgent",
    "ComparativeAnalystAgent",
    "FlightTimelineAgent",
}


def validate_contributing_factors(
    factors: list[ContributingFactor],
    valid_evidence_ids: set[str],
) -> tuple[list[ContributingFactor], list[ContributingFactor]]:
    """
    Split factors into (grounded, unsupported).

    Two-stage validation:
      1. Evidence existence — supporting_evidence must reference known rule_names
         or agent names from the evidence package.
      2. Domain admissibility — if the factor text claims a specific subsystem
         (GPS, EKF, vibration, etc.), the evidence must include at least one ID
         from that domain.  Prevents "GPS failure" claims supported only by
         battery evidence (the bat_anomaly_002 hallucination pattern).

    Grounded: passes both checks.
    Unsupported: fails either check → moved to open_questions.
    Grounded factors are trimmed to only the valid evidence IDs.
    """
    grounded: list[ContributingFactor] = []
    unsupported: list[ContributingFactor] = []

    for f in factors:
        # Stage 1: evidence existence
        matched = [e for e in f.supporting_evidence if e in valid_evidence_ids]
        if not matched:
            unsupported.append(f)
            continue

        # Stage 2: domain admissibility — claim domain must match evidence domain
        admissible, reason = check_factor_domain(f.factor, matched)
        if not admissible:
            # Rebuild with only the matched IDs so the rejection message is accurate
            unsupported.append(ContributingFactor(
                factor=f.factor + f" [domain mismatch: {reason}]",
                supporting_evidence=matched,
                confidence=f.confidence,
            ))
            continue

        grounded.append(ContributingFactor(
            factor=f.factor,
            supporting_evidence=matched,
            confidence=f.confidence,
        ))

    return grounded, unsupported


class CrashInvestigatorAgent(BaseAgent):
    AGENT_NAME = "CrashInvestigatorAgent"
    AGENT_ROLE = "[CRASH] Crash Sequence Investigator"

    def run(self, state: dict) -> dict:
        self.emit(state, "Synthesizing all domain findings for crash investigation...")

        agent_findings = state.get("agent_findings", {})
        hypotheses = state.get("hypotheses", [])
        anomalies = state.get("anomalies", [])
        timeline = self.store.read_derived(self.flight_id, "timeline") or {}
        phases = self.store.read_derived(self.flight_id, "phases") or []

        self.emit(state, f"Evidence: {len(agent_findings)} agents, "
                         f"{len(hypotheses)} hypotheses, {len(anomalies)} anomalies")

        # Build the evidence namespace: all valid IDs the LLM may cite
        valid_evidence_ids: set[str] = (
            {a["rule_name"] for a in anomalies if a.get("rule_name")}
            | {name for name in agent_findings if name in _DOMAIN_AGENT_NAMES}
        )

        evidence_package = self._build_crash_evidence(
            agent_findings, hypotheses, anomalies, timeline, phases,
            valid_evidence_ids, state,
        )

        self.emit(state, "Calling primary LLM for root cause analysis (may take 30-90s)...")

        result: CrashInvestigationResult = self.timed_llm_call(
            self.llm.structured,
            messages=[{"role": "user", "content": evidence_package}],
            response_model=CrashInvestigationResult,
            system=CRASH_INVESTIGATOR_PROMPT,
            model=self.llm.primary_model,
        )

        # Validate contributing factors — stage 1 (existence) + stage 2 (domain)
        grounded, unsupported = validate_contributing_factors(
            result.contributing_factors, valid_evidence_ids
        )

        if unsupported:
            self.emit(
                state,
                f"Grounding check: {len(unsupported)} unsupported/cross-domain factor(s) "
                f"moved to open_questions: {[f.factor[:60] for f in unsupported]}",
                level="warning",
            )

        # Validate proximate_cause domain — detect GPS/EKF claims with no GPS/EKF evidence.
        # If a domain mismatch is found, fall back to the highest-severity anomaly's
        # description so the root_cause is always anchored to real telemetry.
        proximate_cause = result.proximate_cause
        cause_domain_warnings = check_claim_text_domain(proximate_cause, valid_evidence_ids)
        if cause_domain_warnings:
            self.emit(
                state,
                f"Proximate cause domain mismatch ({'; '.join(cause_domain_warnings[:2])}) — "
                f"anchoring to highest-severity anomaly",
                level="warning",
            )
            # Anchor root cause to the highest-severity anomaly in the evidence set
            _sev = {"FATAL": 4, "CRITICAL": 3, "WARNING": 2, "INFO": 1}
            top = max(
                anomalies,
                key=lambda a: (_sev.get(a.get("severity", "INFO"), 0), a.get("timestamp_us", 0)),
                default=None,
            )
            if top:
                proximate_cause = (
                    f"{top['description']} "
                    f"[anchored to {top['rule_name']} at T+{top.get('timestamp_us',0)/1e6:.1f}s; "
                    f"LLM claim contained inadmissible domain reference]"
                )

        self.emit(state, f"Root cause: {proximate_cause[:100]}")
        self.emit(state, f"Confidence: {result.confidence_label} ({result.overall_confidence:.0%})")
        self.emit(state, f"Contributing factors: {len(grounded)} grounded, "
                         f"{len(unsupported)} rejected")

        # Serialize grounded factors as human-readable strings for state/API
        # Format: "Description [evidence: ID1, ID2] (CONFIDENCE)"
        contributing_factors_str = [
            f"{f.factor} [evidence: {', '.join(f.supporting_evidence)}] ({f.confidence})"
            for f in grounded
        ]

        # Rejected factors become open questions
        extra_questions = [
            f"Unsupported claim (no admissible evidence) — needs review: {f.factor}"
            for f in unsupported
        ]

        state["root_cause"] = proximate_cause
        state["confidence"] = result.confidence_label
        state["contributing_factors"] = contributing_factors_str
        state["recommendations"] = [
            f"[{a.priority}] {a.action}"
            + (f" — param: {a.parameter}={a.parameter_value}" if a.parameter else "")
            for a in result.corrective_actions
        ]
        state["open_questions"] = list(dict.fromkeys(
            result.open_questions + extra_questions
        ))

        state["hypotheses"] = [
            h.model_dump() for h in result.root_causes + result.refuted_hypotheses
        ]

        state.setdefault("agent_findings", {})[self.AGENT_NAME] = {
            "proximate_cause": proximate_cause,  # domain-validated; may differ from LLM output
            "root_causes": [h.model_dump() for h in result.root_causes],
            # Store structured form for ReportWriter and downstream consumers
            "contributing_factors": contributing_factors_str,
            "contributing_factors_structured": [f.model_dump() for f in grounded],
            "contributing_factors_unsupported": [f.model_dump() for f in unsupported],
            "refuted_hypotheses": [h.model_dump() for h in result.refuted_hypotheses],
            "causal_chain": result.causal_chain,
            "five_why": result.five_why,
            "overall_confidence": result.overall_confidence,
            "confidence_label": result.confidence_label,
            "corrective_actions": [a.model_dump() for a in result.corrective_actions],
            "open_questions": result.open_questions,
        }

        return state

    def _build_crash_evidence(
        self,
        agent_findings: dict,
        hypotheses: list,
        anomalies: list,
        timeline: dict,
        phases: list,
        valid_evidence_ids: set[str],
        state: dict,
    ) -> str:
        lines = [
            "COMPLETE INVESTIGATION EVIDENCE PACKAGE",
            "=" * 70,
            f"Flight ID: {self.flight_id}",
            f"Query: {state.get('user_query', 'Unknown')}",
            "",
        ]

        # Flight phases
        lines.append("FLIGHT PHASES:")
        for phase in phases:
            dur_s = (phase.get("end_us", 0) - phase.get("start_us", 0)) / 1e6
            lines.append(f"  {phase['name']} ({phase.get('mode_name','?')}): "
                         f"T+{phase.get('start_us',0)/1e6:.1f}s → "
                         f"T+{phase.get('end_us',0)/1e6:.1f}s ({dur_s:.1f}s) "
                         f"{phase.get('notes', '')}")

        # Key events
        lines.append("")
        lines.append("KEY EVENTS (chronological):")
        for event in timeline.get("critical_events", []):
            lines.append(f"  T+{event['timestamp_us']/1e6:.1f}s [{event['severity']}] "
                         f"{event['event_type']}: {event['description']}")

        event_horizon = timeline.get("event_horizon_us")
        if event_horizon:
            lines.append(f"\n  *** EVENT HORIZON (last normal state): T+{event_horizon/1e6:.1f}s ***")

        # Critical anomalies — labelled with exact rule_names
        lines.append("")
        crit_anomalies = sorted(
            [a for a in anomalies if a.get("severity") in ("CRITICAL", "FATAL")],
            key=lambda a: a.get("timestamp_us", 0),
        )
        lines.append(f"CRITICAL ANOMALIES ({len(crit_anomalies)} detected):")
        for a in crit_anomalies[:100]:
            rv = a.get("raw_values", {})
            rv_str = f" {rv}" if rv else ""
            lines.append(f"  [{a['rule_name']}] T+{a.get('timestamp_us',0)/1e6:.1f}s "
                         f"[{a['severity']}] {a.get('category','?')}: "
                         f"{a['description']}{rv_str}")

        # Domain agent summaries — labelled with agent names
        lines.append("")
        lines.append("DOMAIN AGENT FINDINGS (agent names are valid evidence IDs):")
        agent_order = [
            "EKFDiagnosticsAgent", "GPSIntegrityAgent", "PowerSystemAgent",
            "VibrationAnalysisAgent", "ESCMotorAgent", "MissionBehaviorAgent",
            "FlightDynamicsAgent", "ParameterDriftAgent", "SafetyComplianceAgent",
        ]
        for agent_name in agent_order:
            finding = agent_findings.get(agent_name)
            if finding:
                lines.append(f"\n  [{agent_name}]")
                lines.append(f"    Summary: {finding.get('summary', 'No summary available')}")
                for key in ("filter_health", "integrity_score", "brownout_detected",
                            "overall_severity", "power_causal_to_failure",
                            "causal_to_ekf_failure", "violations"):
                    if key in finding:
                        lines.append(f"    {key}: {finding[key]}")

        # Hypotheses
        lines.append("")
        lines.append("HYPOTHESES FROM DOMAIN AGENTS:")
        for h in hypotheses:
            lines.append(f"  [{h.get('status','forming').upper()}] "
                         f"{h.get('title','Unknown')} "
                         f"(confidence={h.get('confidence',0):.0%}, "
                         f"source={h.get('agent_source','?')})")
            lines.append(f"    {h.get('description','')[:200]}")
            for ev in h.get("evidence_for", [])[:3]:
                lines.append(f"    + {ev}")

        # ── Causal weighting section: temporal precedence + cross-agent agreement ──
        # Anomalies are sorted by timestamp above. Here we explicitly call out which
        # anomalies appeared EARLIEST so the LLM weights them as potential root causes
        # rather than symptoms.
        lines.append("")
        lines.append("CAUSAL WEIGHTING GUIDANCE (deterministic ranking):")
        lines.append("  Ranking rules (earlier = more likely root cause, not symptom):")
        lines.append("  1. Direct error/failsafe ERR-log events (highest authority)")
        lines.append("  2. FATAL/CRITICAL anomalies by timestamp (earlier = potential initiator)")
        lines.append("  3. Cross-agent agreement (same subsystem flagged by rule AND agent)")
        lines.append("  4. Parameter misconfiguration (amplifies other failures)")
        lines.append("  5. WARNING anomalies (contextual, rarely root cause alone)")

        # Identify earliest CRITICAL/FATAL anomaly — likely initiating event
        crit_sorted = sorted(
            [a for a in anomalies if a.get("severity") in ("CRITICAL", "FATAL")],
            key=lambda a: a.get("timestamp_us", 0),
        )
        if crit_sorted:
            first = crit_sorted[0]
            lines.append(
                f"\n  EARLIEST CRITICAL/FATAL: [{first['rule_name']}] "
                f"T+{first.get('timestamp_us',0)/1e6:.1f}s — treat as candidate initiating event"
            )
            if len(crit_sorted) > 1:
                last = crit_sorted[-1]
                lines.append(
                    f"  LATEST CRITICAL/FATAL: [{last['rule_name']}] "
                    f"T+{last.get('timestamp_us',0)/1e6:.1f}s — likely symptom of initiating event"
                )

        # Parameter misconfigurations are high-authority causal amplifiers
        param_anomalies = [a for a in anomalies if a.get("rule_name", "").startswith("PARAM_")]
        if param_anomalies:
            lines.append("\n  PARAMETER MISCONFIGURATIONS (amplify failure chains):")
            for p in param_anomalies[:5]:
                lines.append(f"    [{p['rule_name']}] {p['description']}")

        # Domain evidence availability summary — tells LLM which domains have evidence
        # This prevents cross-domain hallucination (GPS claims when only BAT evidence exists)
        lines.append("\n  DOMAIN EVIDENCE AVAILABLE (claim only what's listed here):")
        domain_prefixes = {
            "GPS": "GPS_",
            "EKF": "EKF_",
            "Power/Battery": "BAT_",
            "Vibration/IMU": ("VIBE_", "IMU_"),
            "Motor/ESC": ("MOTOR_", "ESC_"),
            "Failsafe": "FAILSAFE_",
        }
        any_rule = {a.get("rule_name", "") for a in anomalies}
        for domain_label, prefix in domain_prefixes.items():
            prefixes = (prefix,) if isinstance(prefix, str) else prefix
            domain_rules = [r for r in any_rule if any(r.startswith(p) for p in prefixes)]
            domain_agents = [n for n in agent_findings if domain_label.split("/")[0].lower()
                             in n.lower() and n in _DOMAIN_AGENT_NAMES]
            if domain_rules or domain_agents:
                lines.append(
                    f"    {domain_label}: rules={domain_rules[:4]} agents={domain_agents}"
                )
            else:
                lines.append(f"    {domain_label}: NO EVIDENCE — do NOT claim this domain")

        # Explicit evidence namespace — LLM must only cite these IDs
        rule_names = sorted({a["rule_name"] for a in anomalies if a.get("rule_name")})
        agent_names = sorted(
            n for n in agent_findings if n in _DOMAIN_AGENT_NAMES
        )
        lines.append("")
        lines.append("VALID EVIDENCE IDs (use ONLY these in supporting_evidence fields):")
        lines.append("  Anomaly rule_names:")
        for rn in rule_names:
            lines.append(f"    {rn}")
        lines.append("  Agent names:")
        for an in agent_names:
            lines.append(f"    {an}")

        lines.append("")
        lines.append("TASK: Perform complete root cause analysis.")
        lines.append("For each contributing_factor, populate supporting_evidence with")
        lines.append("exact IDs from the VALID EVIDENCE IDs list above.")
        lines.append("DOMAIN CONSTRAINT: if your factor text mentions GPS/satellite/HDOP,")
        lines.append("  you MUST have GPS_ or GPSIntegrityAgent evidence.")
        lines.append("  if your factor text mentions EKF/Kalman/navigation filter,")
        lines.append("  you MUST have EKF_ or EKFDiagnosticsAgent evidence.")
        lines.append("  Factors violating domain constraints will be rejected.")
        lines.append("Do NOT invent rule_names. Do NOT cite evidence not in the list.")

        return "\n".join(lines)
