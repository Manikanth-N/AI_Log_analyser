"""
[EKF] EKF Health & Innovation Diagnostician Agent.
Runs deterministic EKF rules, computes filter health metrics, asks LLM to interpret.
"""

import polars as pl

from intelligence.rules.ekf_rules import ALL_EKF_RULES
from intelligence.signals.statistical import detect_changepoints
from llm.prompts.system_prompts import EKF_AGENT_PROMPT
from llm.structured import EKFDiagnosticResult, EvidenceItem
from parsers.schema import TIMESTAMP_COL
from .base import BaseAgent


class EKFDiagnosticsAgent(BaseAgent):
    AGENT_NAME = "EKFDiagnosticsAgent"
    AGENT_ROLE = "[EKF] EKF Health & Innovation Diagnostician"

    def run(self, state: dict) -> dict:
        self.emit(state, "Starting EKF diagnostics analysis...")

        data = self.load_data("NKF1", "NKF3", "NKF4", "NKF5", "XKF1", "XKF3", "XKF4", "ERR")

        # STEP 1: Run all deterministic EKF rules
        rule_anomalies = []
        for rule in ALL_EKF_RULES:
            try:
                anomalies = rule.evaluate(data)
                rule_anomalies.extend(anomalies)
            except Exception as e:
                self.log.warning("rule_error", rule=rule.RULE_NAME, error=str(e))

        self.emit(state, f"EKF rules: {len(rule_anomalies)} anomalies detected")

        # STEP 2: Compute filter health metrics
        metrics = self._compute_filter_metrics(data)
        self.emit(state, f"EKF metrics computed: max_ivr={metrics.get('max_vel_ivr', 'N/A')}")

        # STEP 3: Changepoint detection on innovation series
        changepoints = self._detect_innovation_changepoints(data)
        if changepoints:
            self.emit(state, f"EKF behavior change detected at {len(changepoints)} timestamps")

        # STEP 4: LLM interpretation
        if rule_anomalies or metrics.get("max_vel_ivr", 0) > 0.3:
            evidence_text = self._build_ekf_evidence(rule_anomalies, metrics, changepoints)
            result = self.timed_llm_call(
                self.llm.structured,
                messages=[{"role": "user", "content": evidence_text}],
                response_model=EKFDiagnosticResult,
                system=EKF_AGENT_PROMPT,
                model=self.llm.fast_model,
            )
            self.emit(state, f"EKF conclusion: {result.filter_health} — {result.summary[:80]}")
        else:
            # No EKF anomalies detected — record as nominal
            result = EKFDiagnosticResult(
                filter_health="HEALTHY",
                confidence=0.90,
                summary="EKF operated within normal parameters throughout flight. No innovation anomalies detected.",
                lane_switch_occurred=False,
                filter_recovered=True,
                position_trustworthy_after_event=True,
            )
            self.emit(state, "EKF: NOMINAL — no filter anomalies detected")

        # STEP 5: Add anomalies and findings to state
        state.setdefault("anomalies", []).extend([
            {
                "rule_name": a.rule_name,
                "category": a.category,
                "severity": a.severity,
                "timestamp_us": a.timestamp_us,
                "end_timestamp_us": a.end_timestamp_us,
                "description": a.description,
                "raw_values": a.raw_values,
                "detected_by": self.AGENT_NAME,
            }
            for a in rule_anomalies
        ])

        state.setdefault("agent_findings", {})[self.AGENT_NAME] = {
            "filter_health": result.filter_health,
            "confidence": result.confidence,
            "summary": result.summary,
            "lane_switch_occurred": result.lane_switch_occurred,
            "position_trustworthy": result.position_trustworthy_after_event,
            "causal_sensor": result.causal_sensor,
            "anomaly_count": len(rule_anomalies),
            "metrics": metrics,
            "changepoints_us": changepoints,
        }

        # Add hypothesis if filter failed
        if result.filter_health in ("DEGRADED", "FAILED"):
            state.setdefault("hypotheses", []).append({
                "id": f"ekf_failure_{self.flight_id[:8]}",
                "title": f"EKF filter {result.filter_health} during flight",
                "description": result.summary,
                "confidence": result.confidence,
                "status": "supported",
                "agent_source": self.AGENT_NAME,
                "evidence_for": [e.context for e in result.evidence],
                "evidence_against": [],
                "missing_evidence": [],
            })

        return state

    def _compute_filter_metrics(self, data: dict) -> dict:
        _nkf4 = data.get("NKF4"); nkf4 = _nkf4 if (_nkf4 is not None and not _nkf4.is_empty()) else data.get("XKF4")
        _nkf3 = data.get("NKF3"); nkf3 = _nkf3 if (_nkf3 is not None and not _nkf3.is_empty()) else data.get("XKF3")

        metrics = {
            "max_vel_ivr": None,
            "max_pos_ivr": None,
            "max_hgt_ivr": None,
            "max_heading_innov_rad": None,
            "lane_switches": 0,
            "mean_vel_ivr": None,
        }

        if nkf4 is not None and not nkf4.is_empty():
            if "var_ratio_vel" in nkf4.columns:
                metrics["max_vel_ivr"] = float(nkf4["var_ratio_vel"].max())
                metrics["mean_vel_ivr"] = float(nkf4["var_ratio_vel"].mean())
            if "var_ratio_pos" in nkf4.columns:
                metrics["max_pos_ivr"] = float(nkf4["var_ratio_pos"].max())
            if "var_ratio_hgt" in nkf4.columns:
                metrics["max_hgt_ivr"] = float(nkf4["var_ratio_hgt"].max())
            if "lane" in nkf4.columns:
                lane_changes = nkf4["lane"].diff().abs().sum()
                metrics["lane_switches"] = int(lane_changes or 0)

        if nkf3 is not None and not nkf3.is_empty():
            if "innov_heading" in nkf3.columns:
                metrics["max_heading_innov_rad"] = float(nkf3["innov_heading"].abs().max())

        return metrics

    def _detect_innovation_changepoints(self, data: dict) -> list[int]:
        import numpy as np
        _nkf4 = data.get("NKF4"); nkf4 = _nkf4 if (_nkf4 is not None and not _nkf4.is_empty()) else data.get("XKF4")
        if nkf4 is None or nkf4.is_empty() or "var_ratio_vel" not in nkf4.columns:
            return []

        values = nkf4["var_ratio_vel"].to_numpy()
        ts = nkf4[TIMESTAMP_COL].to_numpy()
        return detect_changepoints(values, ts, min_size=30, penalty=5.0)

    def _build_ekf_evidence(
        self,
        anomalies: list,
        metrics: dict,
        changepoints: list[int],
    ) -> str:
        lines = ["ANALYZE THE FOLLOWING EKF EVIDENCE:", ""]

        lines.append("DETERMINISTIC RULE FINDINGS:")
        if anomalies:
            for a in anomalies:
                lines.append(f"  [{a.severity}] {a.rule_name}: {a.description} @ T={a.timestamp_us/1e6:.1f}s")
        else:
            lines.append("  No rule violations detected.")

        lines.append("")
        lines.append("COMPUTED FILTER METRICS:")
        for k, v in metrics.items():
            if v is None:
                lines.append(f"  {k}: NOT AVAILABLE IN LOG")
            elif isinstance(v, float):
                lines.append(f"  {k}: {v:.4f}")
            else:
                lines.append(f"  {k}: {v}")

        if changepoints:
            lines.append("")
            lines.append(f"FILTER BEHAVIOR CHANGEPOINTS DETECTED at timestamps (µs): {changepoints}")

        lines.append("")
        lines.append("Based ONLY on this evidence, provide your EKFDiagnosticResult.")
        lines.append("Do not invent values not present above.")

        return "\n".join(lines)
