"""[DYNAMICS] Flight Dynamics & Control Stability Analyst Agent."""

import numpy as np
import polars as pl

from parsers.schema import TIMESTAMP_COL
from .base import BaseAgent


class FlightDynamicsAgent(BaseAgent):
    AGENT_NAME = "FlightDynamicsAgent"
    AGENT_ROLE = "[DYNAMICS] Flight Dynamics & Control Stability Analyst"

    def run(self, state: dict) -> dict:
        self.emit(state, "Analyzing flight dynamics and control stability...")

        data = self.load_data("ATT", "RCOU")
        att_df = data.get("ATT")

        anomalies = []
        metrics = {}

        if att_df is not None and not att_df.is_empty():
            # Check for attitude extremes
            if "roll_deg" in att_df.columns:
                max_roll = float(att_df["roll_deg"].abs().max())
                metrics["max_roll_deg"] = max_roll
                if max_roll > 45:
                    anomalies.append({
                        "rule_name": "ATT_EXTREME_ROLL",
                        "category": "CONTROL",
                        "severity": "CRITICAL" if max_roll > 60 else "WARNING",
                        "timestamp_us": int(att_df.filter(
                            pl.col("roll_deg").abs() == att_df["roll_deg"].abs().max()
                        )[TIMESTAMP_COL][0]),
                        "description": f"Extreme roll attitude: {max_roll:.1f}°",
                        "raw_values": {"max_roll_deg": max_roll},
                        "detected_by": self.AGENT_NAME,
                    })

            if "pitch_deg" in att_df.columns:
                max_pitch = float(att_df["pitch_deg"].abs().max())
                metrics["max_pitch_deg"] = max_pitch

            # Detect oscillation: check if roll/pitch oscillates at > 2 Hz
            if "roll_deg" in att_df.columns and len(att_df) > 100:
                ts_us = att_df[TIMESTAMP_COL].to_numpy()
                roll = att_df["roll_deg"].to_numpy()
                dt_s = np.median(np.diff(ts_us)) / 1e6
                if dt_s > 0:
                    from scipy import signal
                    freqs, psd = signal.welch(roll, fs=1/dt_s, nperseg=min(256, len(roll)//4))
                    # Look for dominant frequency between 2-10 Hz (oscillation range)
                    osc_mask = (freqs >= 2.0) & (freqs <= 10.0)
                    if np.any(osc_mask):
                        osc_power = float(psd[osc_mask].max())
                        metrics["oscillation_power_2_10hz"] = osc_power
                        if osc_power > np.percentile(psd, 90):
                            anomalies.append({
                                "rule_name": "CONTROL_OSCILLATION",
                                "category": "CONTROL",
                                "severity": "WARNING",
                                "timestamp_us": int(ts_us[0]),
                                "description": f"Roll oscillation detected in 2-10 Hz band (PID instability indicator)",
                                "raw_values": {"osc_power": osc_power},
                                "detected_by": self.AGENT_NAME,
                            })

        state.setdefault("anomalies", []).extend(anomalies)
        state.setdefault("agent_findings", {})[self.AGENT_NAME] = {
            "confidence": 0.75,
            "summary": (
                f"Control stability: max_roll={metrics.get('max_roll_deg', 'N/A')}°, "
                f"max_pitch={metrics.get('max_pitch_deg', 'N/A')}°. "
                f"{'Anomalies: ' + str(len(anomalies)) if anomalies else 'Control dynamics nominal.'}"
            ),
            "metrics": metrics,
            "anomaly_count": len(anomalies),
        }

        return state
