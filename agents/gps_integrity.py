"""[GPS] GPS Integrity & Spoofing Analyst Agent."""

from intelligence.rules.gps_rules import ALL_GPS_RULES
from llm.prompts.system_prompts import GPS_AGENT_PROMPT
from llm.structured import GPSIntegrityResult
from .base import BaseAgent


class GPSIntegrityAgent(BaseAgent):
    AGENT_NAME = "GPSIntegrityAgent"
    AGENT_ROLE = "[GPS] GPS Integrity & Spoofing Analyst"

    def run(self, state: dict) -> dict:
        self.emit(state, "Starting GPS integrity analysis...")

        data = self.load_data("GPS", "GPS2")

        # Deterministic rules
        rule_anomalies = []
        for rule in ALL_GPS_RULES:
            try:
                rule_anomalies.extend(rule.evaluate(data))
            except Exception as e:
                self.log.warning("rule_error", rule=rule.RULE_NAME, error=str(e))

        self.emit(state, f"GPS rules: {len(rule_anomalies)} anomalies")

        # Compute GPS metrics
        metrics = self._compute_gps_metrics(data.get("GPS"))

        # LLM interpretation
        if rule_anomalies or (metrics.get("hdop_max", 0) or 0) > 2.0:
            evidence = self._build_evidence(rule_anomalies, metrics)
            result = self.timed_llm_call(
                self.llm.structured,
                messages=[{"role": "user", "content": evidence}],
                response_model=GPSIntegrityResult,
                system=GPS_AGENT_PROMPT,
                model=self.llm.fast_model,
            )
            self.emit(state, f"GPS integrity score: {result.integrity_score:.0f}/100 — {result.summary[:80]}")
        else:
            result = GPSIntegrityResult(
                integrity_score=95.0,
                confidence=0.90,
                summary="GPS operated nominally throughout flight. No integrity issues detected.",
            )
            self.emit(state, "GPS: NOMINAL")

        state.setdefault("anomalies", []).extend([
            {"rule_name": a.rule_name, "category": a.category, "severity": a.severity,
             "timestamp_us": a.timestamp_us, "description": a.description,
             "raw_values": a.raw_values, "detected_by": self.AGENT_NAME}
            for a in rule_anomalies
        ])

        state.setdefault("agent_findings", {})[self.AGENT_NAME] = {
            "integrity_score": result.integrity_score,
            "confidence": result.confidence,
            "summary": result.summary,
            "glitches_detected": result.glitches_detected,
            "spoofing_likelihood": result.spoofing_likelihood,
            "hdop_max": metrics.get("hdop_max"),
            "sat_count_min": metrics.get("sat_count_min"),
            "causal_to_ekf_failure": result.causal_to_ekf_failure,
            "degradation_start_us": result.degradation_start_us,
        }

        if result.integrity_score < 50 or result.causal_to_ekf_failure:
            state.setdefault("hypotheses", []).append({
                "id": f"gps_degradation_{self.flight_id[:8]}",
                "title": f"GPS degradation (score={result.integrity_score:.0f}/100)",
                "description": result.summary,
                "confidence": result.confidence,
                "status": "supported",
                "agent_source": self.AGENT_NAME,
                "evidence_for": [a.description for a in rule_anomalies if a.severity == "CRITICAL"],
                "evidence_against": [],
                "missing_evidence": [],
            })

        return state

    def _compute_gps_metrics(self, gps_df) -> dict:
        if gps_df is None or gps_df.is_empty():
            return {}
        metrics = {}
        if "hdop" in gps_df.columns:
            metrics["hdop_mean"] = float(gps_df["hdop"].mean())
            metrics["hdop_max"] = float(gps_df["hdop"].max())
            metrics["hdop_min"] = float(gps_df["hdop"].min())
        if "num_sats" in gps_df.columns:
            metrics["sat_count_min"] = int(gps_df["num_sats"].min())
            metrics["sat_count_mean"] = float(gps_df["num_sats"].mean())
        if "fix_type" in gps_df.columns:
            metrics["fix_type_min"] = int(gps_df["fix_type"].min())
        if "speed_acc_m_s" in gps_df.columns:
            metrics["speed_acc_max"] = float(gps_df["speed_acc_m_s"].max())
        return metrics

    def _build_evidence(self, anomalies, metrics) -> str:
        lines = ["ANALYZE GPS INTEGRITY EVIDENCE:", ""]
        lines.append("RULE VIOLATIONS:")
        for a in anomalies:
            lines.append(f"  [{a.severity}] {a.rule_name}: {a.description} @ T={a.timestamp_us/1e6:.1f}s")
        if not anomalies:
            lines.append("  None")
        lines.append("\nGPS METRICS:")
        for k, v in metrics.items():
            lines.append(f"  {k}: {v if v is not None else 'NOT AVAILABLE IN LOG'}")
        lines.append("\nProvide GPSIntegrityResult based ONLY on this evidence.")
        return "\n".join(lines)
