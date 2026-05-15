"""[POWER] Power Rail & Battery Forensics Agent."""

from intelligence.rules.power_rules import ALL_POWER_RULES
from llm.prompts.system_prompts import POWER_AGENT_PROMPT
from llm.structured import PowerSystemResult
from .base import BaseAgent


class PowerSystemAgent(BaseAgent):
    AGENT_NAME = "PowerSystemAgent"
    AGENT_ROLE = "[POWER] Power Rail & Battery Forensics"

    def run(self, state: dict) -> dict:
        self.emit(state, "Starting power system analysis...")

        data = self.load_data("BAT", "CURR")

        rule_anomalies = []
        for rule in ALL_POWER_RULES:
            try:
                rule_anomalies.extend(rule.evaluate(data))
            except Exception as e:
                self.log.warning("rule_error", rule=rule.RULE_NAME, error=str(e))

        _bat = data.get("BAT"); _bat_df = _bat if (_bat is not None and not _bat.is_empty()) else data.get("CURR")
        metrics = self._compute_power_metrics(_bat_df)
        self.emit(state, f"Power rules: {len(rule_anomalies)} anomalies. Min V: {metrics.get('voltage_min', 'N/A')}")

        if rule_anomalies:
            evidence = self._build_evidence(rule_anomalies, metrics)
            result = self.timed_llm_call(
                self.llm.structured,
                messages=[{"role": "user", "content": evidence}],
                response_model=PowerSystemResult,
                system=POWER_AGENT_PROMPT,
                model=self.llm.fast_model,
            )
            self.emit(state, f"Power: brownout={result.brownout_detected}, causal={result.power_causal_to_failure}")
        else:
            result = PowerSystemResult(
                confidence=0.92,
                summary="Power system nominal. No voltage anomalies, current spikes, or brownout signatures detected.",
                power_causal_to_failure=False,
                brownout_detected=False,
                min_voltage_v=metrics.get("voltage_min"),
                max_current_a=metrics.get("current_max"),
            )
            self.emit(state, "POWER: NOMINAL")

        state.setdefault("anomalies", []).extend([
            {"rule_name": a.rule_name, "category": a.category, "severity": a.severity,
             "timestamp_us": a.timestamp_us, "description": a.description,
             "raw_values": a.raw_values, "detected_by": self.AGENT_NAME}
            for a in rule_anomalies
        ])

        state.setdefault("agent_findings", {})[self.AGENT_NAME] = {
            "confidence": result.confidence,
            "summary": result.summary,
            "brownout_detected": result.brownout_detected,
            "power_causal_to_failure": result.power_causal_to_failure,
            "metrics": metrics,
            "anomaly_count": len(rule_anomalies),
        }

        return state

    def _compute_power_metrics(self, bat_df) -> dict:
        if bat_df is None or bat_df.is_empty():
            return {}
        m = {}
        if "voltage_v" in bat_df.columns:
            m["voltage_min"] = float(bat_df["voltage_v"].min())
            m["voltage_max"] = float(bat_df["voltage_v"].max())
            m["voltage_mean"] = float(bat_df["voltage_v"].mean())
        if "current_a" in bat_df.columns:
            m["current_max"] = float(bat_df["current_a"].max())
            m["current_mean"] = float(bat_df["current_a"].mean())
        if "remaining_pct" in bat_df.columns:
            m["remaining_min_pct"] = float(bat_df["remaining_pct"].min())
        if "consumed_mah" in bat_df.columns:
            m["consumed_mah_total"] = float(bat_df["consumed_mah"].max())
        return m

    def _build_evidence(self, anomalies, metrics) -> str:
        lines = ["POWER SYSTEM EVIDENCE:", ""]
        lines.append("RULE VIOLATIONS:")
        for a in anomalies:
            lines.append(f"  [{a.severity}] {a.rule_name}: {a.description}")
        lines.append("\nPOWER METRICS:")
        for k, v in metrics.items():
            lines.append(f"  {k}: {v if v is not None else 'NOT AVAILABLE'}")
        lines.append("\nProvide PowerSystemResult based ONLY on this evidence.")
        return "\n".join(lines)
