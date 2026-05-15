"""[PARAMS] Parameter Baseline Drift Detector Agent."""

import polars as pl

from llm.prompts.system_prompts import PARAMETER_DRIFT_PROMPT
from parsers.schema import TIMESTAMP_COL
from .base import BaseAgent

# Acceptable parameter ranges for typical ArduPilot multirotor
_DEFAULT_PROFILE = {
    "EK3_CHECK_SCALE": (0.1, 2.0),
    "GPS_HDOP_GOOD": (1.0, 2.5),
    "BATT_LOW_VOLT": (13.0, 14.4),      # 4S: 3.25-3.6V/cell
    "BATT_CRT_VOLT": (12.4, 13.6),      # 4S: 3.1-3.4V/cell
    "FS_THR_ENABLE": (1, 3),
    "INS_GYRO_FILTER": (10, 50),
    "INS_ACCEL_FILTER": (10, 50),
}


class ParameterDriftAgent(BaseAgent):
    AGENT_NAME = "ParameterDriftAgent"
    AGENT_ROLE = "[PARAMS] Parameter Baseline Drift Detector"

    def run(self, state: dict) -> dict:
        self.emit(state, "Analyzing flight parameters for drift or misconfiguration...")

        parm_df = self.store.load(self.flight_id, "PARM")
        if parm_df.is_empty() or "name" not in parm_df.columns:
            self.emit(state, "WARNING: No PARM messages in log — cannot verify parameters")
            state.setdefault("agent_findings", {})[self.AGENT_NAME] = {
                "confidence": 0.20,
                "summary": "Parameter data not available in this log.",
                "data_gap": "PARM messages absent",
                "violations": [],
            }
            return state

        # Build param dict from initial dump (first 30 seconds)
        t_start = int(parm_df[TIMESTAMP_COL].min() or 0)
        initial_params = parm_df.filter(
            pl.col(TIMESTAMP_COL) <= t_start + 30_000_000
        )

        param_dict = {}
        for row in initial_params.iter_rows(named=True):
            name = row.get("name")
            value = row.get("value")
            if name and value is not None:
                param_dict[name] = float(value)

        # Check against profile
        violations = []
        for param_name, (min_val, max_val) in _DEFAULT_PROFILE.items():
            if param_name not in param_dict:
                violations.append({
                    "param": param_name,
                    "issue": "MISSING",
                    "severity": "INFO",
                    "expected": f"{min_val}–{max_val}",
                    "actual": "NOT IN LOG",
                })
                continue
            val = param_dict[param_name]
            if not (min_val <= val <= max_val):
                severity = "WARNING"
                if param_name in ("BATT_LOW_VOLT", "BATT_CRT_VOLT", "FS_THR_ENABLE"):
                    severity = "CRITICAL"
                violations.append({
                    "param": param_name,
                    "issue": "OUT_OF_RANGE",
                    "severity": severity,
                    "expected": f"{min_val}–{max_val}",
                    "actual": val,
                })

        self.emit(state, f"Parameters: {len(param_dict)} logged, {len(violations)} violations")

        for v in violations:
            if v["issue"] == "OUT_OF_RANGE":
                state.setdefault("anomalies", []).append({
                    "rule_name": "PARAM_OUT_OF_RANGE",
                    "category": "PARAMETERS",
                    "severity": v["severity"],
                    "timestamp_us": t_start,
                    "description": f"Parameter {v['param']} = {v['actual']} (expected {v['expected']})",
                    "raw_values": {"param": v["param"], "value": v["actual"]},
                    "detected_by": self.AGENT_NAME,
                })

        state.setdefault("agent_findings", {})[self.AGENT_NAME] = {
            "confidence": 0.85,
            "summary": (
                f"Checked {len(_DEFAULT_PROFILE)} critical parameters. "
                f"{len(violations)} violations found." if violations else
                f"All checked parameters within acceptable ranges."
            ),
            "total_params": len(param_dict),
            "violations": violations,
            "anomaly_count": len(violations),
        }

        return state
