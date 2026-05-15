"""[MISSION] Mission Logic & Failsafe Verifier Agent."""

from intelligence.rules.failsafe_rules import ALL_FAILSAFE_RULES
from llm.prompts.system_prompts import MISSION_AGENT_PROMPT
from .base import BaseAgent


class MissionBehaviorAgent(BaseAgent):
    AGENT_NAME = "MissionBehaviorAgent"
    AGENT_ROLE = "[MISSION] Mission Logic & Failsafe Verifier"

    def run(self, state: dict) -> dict:
        self.emit(state, "Verifying mission logic and failsafe behavior...")

        data = self.load_data("CMD", "MODE", "ERR", "GPS")

        rule_anomalies = []
        for rule in ALL_FAILSAFE_RULES:
            try:
                rule_anomalies.extend(rule.evaluate(data))
            except Exception as e:
                self.log.warning("rule_error", rule=rule.RULE_NAME, error=str(e))

        self.emit(state, f"Failsafe/mission rules: {len(rule_anomalies)} anomalies")

        # Count mission commands executed
        cmd_df = data.get("CMD")
        cmd_count = 0
        if cmd_df is not None and not cmd_df.is_empty():
            cmd_count = len(cmd_df)

        state.setdefault("anomalies", []).extend([
            {"rule_name": a.rule_name, "category": a.category, "severity": a.severity,
             "timestamp_us": a.timestamp_us, "description": a.description,
             "raw_values": a.raw_values, "detected_by": self.AGENT_NAME}
            for a in rule_anomalies
        ])

        state.setdefault("agent_findings", {})[self.AGENT_NAME] = {
            "confidence": 0.80,
            "summary": (
                f"Mission: {cmd_count} commands logged. "
                f"{'Failsafe anomalies detected: ' + str(len(rule_anomalies)) if rule_anomalies else 'Failsafe behavior nominal.'}"
            ),
            "anomaly_count": len(rule_anomalies),
            "cmd_count": cmd_count,
        }

        return state
