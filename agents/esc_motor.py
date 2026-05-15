"""[ESC] ESC/Motor Behavior & Desync Analyst Agent."""

from intelligence.rules.motor_rules import ALL_MOTOR_RULES
from llm.prompts.system_prompts import ESC_AGENT_PROMPT
from .base import BaseAgent


class ESCMotorAgent(BaseAgent):
    AGENT_NAME = "ESCMotorAgent"
    AGENT_ROLE = "[ESC] ESC/Motor Behavior & Desync Analyst"

    def run(self, state: dict) -> dict:
        self.emit(state, "Starting ESC/motor analysis...")

        data = self.load_data("RCOU", "ESC", "ATT")

        rule_anomalies = []
        for rule in ALL_MOTOR_RULES:
            try:
                rule_anomalies.extend(rule.evaluate(data))
            except Exception as e:
                self.log.warning("rule_error", rule=rule.RULE_NAME, error=str(e))

        has_esc_telemetry = data.get("ESC") is not None and not data["ESC"].is_empty()

        if not has_esc_telemetry:
            self.emit(state, "WARNING: No ESC telemetry in log — cannot verify desync or RPM. Enable BLHeli passthrough logging.")
            state.setdefault("agent_findings", {})[self.AGENT_NAME] = {
                "confidence": 0.40,
                "summary": "ESC telemetry unavailable. Motor output imbalance and desync cannot be fully assessed. Enable BLHeli/KISS ESC logging.",
                "has_esc_telemetry": False,
                "anomaly_count": len(rule_anomalies),
                "data_gap": "ESC RPM, voltage, current, temperature",
            }
        else:
            self.emit(state, f"ESC rules: {len(rule_anomalies)} anomalies")
            state.setdefault("agent_findings", {})[self.AGENT_NAME] = {
                "confidence": 0.80,
                "summary": f"ESC/Motor analysis: {len(rule_anomalies)} anomalies detected." if rule_anomalies else "ESC/Motor nominal.",
                "has_esc_telemetry": True,
                "anomaly_count": len(rule_anomalies),
            }

        state.setdefault("anomalies", []).extend([
            {"rule_name": a.rule_name, "category": a.category, "severity": a.severity,
             "timestamp_us": a.timestamp_us, "description": a.description,
             "raw_values": a.raw_values, "detected_by": self.AGENT_NAME}
            for a in rule_anomalies
        ])

        return state
