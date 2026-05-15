"""Comparative Analyst — compares flight metrics against historical baselines."""

from __future__ import annotations

import json
from typing import Any

import structlog

from agents.base import BaseAgent
from storage.vector_db import VectorStore

InvestigationState = dict  # avoid circular import; state is typed as dict at runtime

log = structlog.get_logger(__name__)


class ComparativeAnalystAgent(BaseAgent):
    """Compares current flight metrics against Qdrant baseline population."""

    AGENT_NAME = "ComparativeAnalystAgent"
    AGENT_ROLE = "[COMPARATIVE] Historical Baseline Analyst"

    def __init__(self, flight_id: str, investigation_id: str, store=None, llm=None):
        super().__init__(flight_id, investigation_id, store=store, llm=llm)
        self.vector_store = VectorStore()

    def run(self, state: InvestigationState) -> InvestigationState:
        self.emit(state, "Comparing flight metrics against baseline population")

        # Gather key scalar metrics from agent_findings
        metrics = self._extract_metrics(state)
        if not metrics:
            self.emit(state, "Insufficient metrics for baseline comparison — skipping")
            state["agent_findings"][self.AGENT_NAME] = {
                "summary": "No metrics available for comparison",
                "deviation": {},
                "anomalous_vs_baselines": [],
            }
            return state

        # Generate embedding for similarity search
        try:
            import json as _json
            metrics_text = _json.dumps(metrics, sort_keys=True)
            embedding = self.llm.embed(metrics_text)
        except Exception as e:
            log.warning("embedding_failed", error=str(e))
            embedding = None

        if embedding is None:
            self.emit(state, "Embedding unavailable — baseline comparison skipped")
            state["agent_findings"][self.AGENT_NAME] = {
                "summary": "Baseline comparison skipped (embedding unavailable)",
                "deviation": {},
                "anomalous_vs_baselines": [],
            }
            return state

        # Compare against baselines
        try:
            import numpy as np
            comparison = self.vector_store.compare_to_baselines(
                current_metrics=metrics,
                current_embedding=np.array(embedding, dtype=np.float32),
                vehicle_type=None,
                phase=None,
            )
        except Exception as e:
            log.warning("baseline_comparison_failed", error=str(e))
            self.emit(state, f"Baseline comparison unavailable: {e}")
            state["agent_findings"][self.AGENT_NAME] = {
                "summary": "Baseline comparison skipped (insufficient historical data)",
                "deviation": {},
                "anomalous_vs_baselines": [],
            }
            return state

        anomalous = [
            k for k, v in comparison.get("z_scores", {}).items()
            if isinstance(v, (int, float)) and abs(v) > 2.5
        ]

        summary_parts = []
        if anomalous:
            summary_parts.append(f"Metrics outside 2.5σ of baseline: {', '.join(anomalous)}")
        elif comparison.get("error"):
            summary_parts.append(f"Comparison unavailable: {comparison['error']}")
        else:
            summary_parts.append("All metrics within normal baseline range (±2.5σ)")

        if comparison.get("similar_flights"):
            summary_parts.append(f"Compared against {len(comparison['similar_flights'])} baseline flights")

        summary = ". ".join(summary_parts)
        self.emit(state, summary)

        state["agent_findings"][self.AGENT_NAME] = {
            "summary": summary,
            "deviation": comparison.get("z_scores", {}),
            "baseline_count": len(comparison.get("similar_flights", [])),
            "anomalous_vs_baselines": anomalous,
        }
        return state

    def _extract_metrics(self, state: InvestigationState) -> dict[str, float]:
        metrics: dict[str, float] = {}
        findings = state.get("agent_findings", {})

        power_metrics = findings.get("PowerSystemAgent", {}).get("metrics", {})
        if power_metrics.get("voltage_min") is not None:
            metrics["battery_voltage_min"] = float(power_metrics["voltage_min"])
        if power_metrics.get("current_max") is not None:
            metrics["current_max_a"] = float(power_metrics["current_max"])

        gps = findings.get("GPSIntegrityAgent", {})
        if gps.get("integrity_score") is not None:
            metrics["gps_integrity_score"] = float(gps["integrity_score"])
        if gps.get("hdop_max") is not None:
            metrics["gps_hdop_max"] = float(gps["hdop_max"])

        ekf_metrics = findings.get("EKFDiagnosticsAgent", {}).get("metrics", {})
        if ekf_metrics.get("max_vel_ivr") is not None:
            metrics["ekf_var_ratio_max"] = float(ekf_metrics["max_vel_ivr"])

        vibe = findings.get("VibrationAnalysisAgent", {})
        if vibe.get("rms_z") is not None:
            metrics["vibration_rms_z"] = float(vibe["rms_z"])

        return metrics
