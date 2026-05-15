"""
[REPORT] Root Cause Report Writer Agent.
Synthesizes all findings into a structured engineering report.

Evidence integrity contract:
  - anomaly_registry is built DETERMINISTICALLY from state["anomalies"]
  - flight_phase_timeline is built DETERMINISTICALLY from state["flight_phases"]
  - The LLM only generates narrative/synthesis (ForensicReportLLM schema)
  - LLMs interpret evidence; they do not invent evidence registries
"""

import json
from datetime import datetime

from llm.prompts.system_prompts import REPORT_WRITER_PROMPT
from llm.structured import ForensicReportLLM
from .base import BaseAgent


class ReportWriterAgent(BaseAgent):
    AGENT_NAME = "ReportWriterAgent"
    AGENT_ROLE = "[REPORT] Root Cause Report Writer"

    def run(self, state: dict) -> dict:
        self.emit(state, "Composing final forensic investigation report...")

        agent_findings = state.get("agent_findings", {})
        anomalies = state.get("anomalies", [])
        phases = state.get("flight_phases", [])

        # Determine classification from deterministic signals
        crash_detected = agent_findings.get("FlightTimelineAgent", {}).get("crash_detected", False)
        classification = "CRASH" if crash_detected else ("ANOMALY" if anomalies else "REVIEW")

        # Build evidence context for LLM (narrative synthesis only)
        evidence = self._build_report_evidence(state, classification)

        self.emit(state, "Generating executive summary and causal chain narrative...")

        llm_report: ForensicReportLLM = self.timed_llm_call(
            self.llm.structured,
            messages=[{"role": "user", "content": evidence}],
            response_model=ForensicReportLLM,
            system=REPORT_WRITER_PROMPT,
            model=self.llm.primary_model,
        )

        # Build deterministic sections — LLM never touches these
        anomaly_registry = _build_anomaly_registry(anomalies)
        phase_timeline = _build_phase_timeline(phases)

        # Assemble complete report dict
        report_data = llm_report.model_dump()
        report_data["flight_phase_timeline"] = phase_timeline
        report_data["anomaly_registry"] = anomaly_registry
        report_data["generated_at"] = datetime.utcnow().isoformat()
        report_data["investigation_id"] = self.investigation_id
        report_data["flight_id"] = self.flight_id

        # Override LLM-generated fields with grounded outputs from deterministic signals
        # and CrashInvestigatorAgent. The LLM's versions are discarded because:
        #   - classification: crash_detected is a physical sensor signal — always wins when True
        #   - contributing_factors: LLM discards [evidence:] citations and can hallucinate
        #   - root_cause_determination: LLM ignores crash_findings and re-investigates
        #   - confidence_level: CrashInvestigator's calibrated label is more reliable
        # All are already validated against the evidence set before reaching this point.

        # Classification: two deterministic guards applied in order.
        #
        # Positive guard (crash_detected=True → must be CRASH):
        #   FlightTimelineAgent firing is a physical sensor signal — authoritative.
        #
        # Inverse guard (crash_detected=False + LLM says CRASH → check evidence):
        #   The LLM may correctly identify crashes the detector missed (e.g. hard
        #   landings: bat_crash_005 has IMU_RAW_EXTREME + EKF_VEL_INNOV_SPIKE).
        #   But the LLM must NOT promote a pure anomaly to CRASH with no crash-class
        #   evidence — this is the bat_anomaly_004 failure mode.
        #   Crash-class evidence: any FATAL anomaly, or a rule that directly indicates
        #   structural impact / navigation failure / propulsion failure.
        _CRASH_CLASS_RULES = frozenset({
            "EKF_LANE_SWITCH", "EKF_VEL_INNOV_SPIKE", "EKF_POS_OFFSET_GROWTH",
            "IMU_RAW_EXTREME", "MOTOR_OUTPUT_DROP", "ESC_DESYNC",
            "FAILSAFE_VERIFY", "UNSAFE_ARM",
        })
        anomaly_rule_names = {a.get("rule_name", "") for a in anomalies}
        has_fatal = any(a.get("severity") == "FATAL" for a in anomalies)
        has_crash_class_evidence = bool(anomaly_rule_names & _CRASH_CLASS_RULES)

        if crash_detected:
            report_data["classification"] = "CRASH"
        elif report_data.get("classification") == "CRASH" and not has_fatal and not has_crash_class_evidence:
            report_data["classification"] = "ANOMALY"
            self.emit(
                state,
                "Classification downgraded CRASH→ANOMALY: no crash-class evidence "
                f"(anomalies: {sorted(anomaly_rule_names)[:5]})",
                level="warning",
            )

        grounded_cfs = state.get("contributing_factors", [])
        if grounded_cfs:
            report_data["contributing_factors"] = grounded_cfs

        crash_findings = state.get("agent_findings", {}).get("CrashInvestigatorAgent", {})
        if crash_findings.get("proximate_cause"):
            report_data["root_cause_determination"] = crash_findings["proximate_cause"]
        if crash_findings.get("confidence_label"):
            report_data["confidence_level"] = crash_findings["confidence_label"]

        report_path = self.store.write_derived(
            self.flight_id,
            f"report_{self.investigation_id}",
            report_data,
        )

        self.emit(state, f"Report saved: {report_path}")
        final_classification = report_data["classification"]
        final_confidence = report_data["confidence_level"]
        self.emit(state, f"Classification: {final_classification} | Confidence: {final_confidence}")
        self.emit(state, f"Executive summary: {llm_report.executive_summary[:120]}...")
        self.emit(state, f"Anomaly registry: {len(anomaly_registry)} entries (deterministic)")

        state["final_report"] = report_data
        state["report_path"] = str(report_path)
        state.setdefault("agent_findings", {})[self.AGENT_NAME] = {
            "report_path": str(report_path),
            "classification": final_classification,
            "confidence_level": final_confidence,
            "executive_summary": llm_report.executive_summary,
            "anomaly_registry_count": len(anomaly_registry),
        }

        return state

    def _build_report_evidence(self, state: dict, classification: str) -> str:
        agent_findings = state.get("agent_findings", {})
        crash_findings = agent_findings.get("CrashInvestigatorAgent", {})
        phases = state.get("flight_phases", [])
        anomalies = state.get("anomalies", [])
        hypotheses = state.get("hypotheses", [])

        lines = ["COMPLETE INVESTIGATION PACKAGE FOR REPORT GENERATION", "=" * 70]
        lines.append(f"\nFLIGHT: {self.flight_id}")
        lines.append(f"QUERY: {state.get('user_query', 'General investigation')}")
        lines.append(f"CLASSIFICATION (pre-determined): {classification}")
        lines.append(f"ROOT CAUSE: {crash_findings.get('proximate_cause', 'See hypotheses')}")
        lines.append(f"CONFIDENCE: {crash_findings.get('confidence_label', 'MEDIUM')}")

        lines.append("\nFLIGHT PHASES (pre-populated — do not regenerate):")
        for p in phases:
            dur = (p.get("end_us", 0) - p.get("start_us", 0)) / 1e6
            lines.append(f"  {p['name']} ({p.get('mode_name','?')}): "
                         f"T+{p.get('start_us',0)/1e6:.1f}s to T+{p.get('end_us',0)/1e6:.1f}s ({dur:.1f}s)")

        # Anomalies formatted with EXACT rule names — the LLM must cite these
        lines.append(f"\nDETECTED ANOMALIES ({len(anomalies)} total — cite rule_names EXACTLY as shown):")
        crit_anoms = sorted(
            [a for a in anomalies if a.get("severity") in ("CRITICAL", "FATAL")],
            key=lambda a: a.get("timestamp_us", 0),
        )
        other_anoms = sorted(
            [a for a in anomalies if a.get("severity") not in ("CRITICAL", "FATAL")],
            key=lambda a: a.get("timestamp_us", 0),
        )
        for a in crit_anoms:
            rv = a.get("raw_values", {})
            rv_str = f" {rv}" if rv else ""
            lines.append(f"  [{a['rule_name']}] T+{a.get('timestamp_us',0)/1e6:.1f}s "
                         f"{a['severity']} ({a.get('category','?')}): {a['description']}{rv_str}")
        # Show first 20 of lower-severity so the LLM sees the distribution
        for a in other_anoms[:20]:
            lines.append(f"  [{a['rule_name']}] T+{a.get('timestamp_us',0)/1e6:.1f}s "
                         f"{a['severity']} ({a.get('category','?')}): {a['description']}")
        if len(other_anoms) > 20:
            lines.append(f"  ... and {len(other_anoms)-20} additional WARNING/INFO anomalies")

        lines.append("\nROOT CAUSES (from CrashInvestigator):")
        for rc in crash_findings.get("root_causes", []):
            lines.append(f"  [{rc.get('status','?').upper()}] {rc.get('title','?')} "
                         f"(confidence={rc.get('confidence',0):.0%})")
            lines.append(f"    {rc.get('description','')[:300]}")
            for ev in rc.get("evidence_for", []):
                lines.append(f"    + {ev}")

        # Use structured form when available so the LLM sees evidence citations
        structured_cfs = crash_findings.get("contributing_factors_structured", [])
        if structured_cfs:
            lines.append("\nCONTRIBUTING FACTORS (grounded — cite these evidence IDs):")
            for cf in structured_cfs:
                ev_str = ", ".join(cf.get("supporting_evidence", []))
                lines.append(f"  [{cf.get('confidence','?')}] {cf['factor']}")
                lines.append(f"    Evidence: {ev_str}")
        else:
            lines.append("\nCONTRIBUTING FACTORS:")
            for cf in crash_findings.get("contributing_factors", []):
                lines.append(f"  - {cf}")

        lines.append("\nCAUSAL CHAIN:")
        lines.append(crash_findings.get("causal_chain", "See root causes above"))

        lines.append("\n5-WHY ANALYSIS:")
        for i, why in enumerate(crash_findings.get("five_why", []), 1):
            lines.append(f"  Why {i}: {why}")

        lines.append("\nCORRECTIVE ACTIONS (from CrashInvestigator):")
        for ca in crash_findings.get("corrective_actions", []):
            lines.append(f"  [{ca.get('priority','?')}] {ca.get('action','?')}")
            if ca.get("parameter"):
                lines.append(f"    Parameter: {ca['parameter']} = {ca.get('parameter_value','?')}")
            lines.append(f"    Rationale: {ca.get('rationale','')}")

        lines.append("\nOPEN QUESTIONS:")
        seen_q = set()
        for oq in crash_findings.get("open_questions", []) + state.get("open_questions", []):
            if oq not in seen_q:
                seen_q.add(oq)
                lines.append(f"  - {oq}")

        lines.append("\nDOMAIN AGENT SUMMARIES (use these to ground contributing factors):")
        for agent_name, findings in agent_findings.items():
            if agent_name in ("CrashInvestigatorAgent", "ReportWriterAgent", "FlightTimelineAgent"):
                continue
            summary = findings.get("summary", "")
            if summary:
                lines.append(f"  [{agent_name}]: {summary}")

        lines.append(
            "\nGENERATE REPORT: Synthesize the above into a forensic report. "
            "Use the exact anomaly rule_names shown. Do not invent new anomaly IDs. "
            "Cite only contributing factors that appear in the domain agent summaries or "
            "root causes above."
        )
        return "\n".join(lines)


# ── deterministic builders ────────────────────────────────────────────────────

def _build_anomaly_registry(anomalies: list[dict]) -> list[dict]:
    """
    Build a deduplicated, severity-sorted anomaly registry from detector output.
    Rule names, timestamps, and descriptions come ONLY from the rules engine.
    """
    sev_order = {"FATAL": 0, "CRITICAL": 1, "WARNING": 2, "INFO": 3}
    seen: set[tuple] = set()
    entries: list[dict] = []
    for a in sorted(anomalies, key=lambda x: (sev_order.get(x.get("severity", "INFO"), 3),
                                               x.get("timestamp_us", 0))):
        key = (a.get("rule_name"), a.get("timestamp_us"))
        if key in seen:
            continue
        seen.add(key)
        entries.append({
            "timestamp_us": a.get("timestamp_us", 0),
            "subsystem": a.get("category", "UNKNOWN"),
            "severity": a.get("severity", "INFO"),
            "description": a.get("description", ""),
            "rule_name": a.get("rule_name", "UNKNOWN"),
            "raw_values": a.get("raw_values", {}),
        })
    return entries


def _build_phase_timeline(phases: list[dict]) -> list[dict]:
    """Build phase timeline from state["flight_phases"] — deterministic."""
    return [
        {
            "name": p.get("name", "UNKNOWN"),
            "start_us": p.get("start_us", 0),
            "end_us": p.get("end_us", 0),
            "mode_name": p.get("mode_name", "UNKNOWN"),
            "notes": p.get("notes", ""),
        }
        for p in phases
    ]
