"""[VIB] Vibration & Mechanical Resonance Analyst Agent."""

from intelligence.rules.vibration_rules import ALL_VIBRATION_RULES
from intelligence.signals.fft_analyzer import analyze_vibration
from llm.prompts.system_prompts import VIBRATION_AGENT_PROMPT
from llm.structured import VibrationResult
from .base import BaseAgent


class VibrationAnalysisAgent(BaseAgent):
    AGENT_NAME = "VibrationAnalysisAgent"
    AGENT_ROLE = "[VIB] Vibration & Mechanical Resonance Analyst"

    def run(self, state: dict) -> dict:
        self.emit(state, "Starting vibration analysis (FFT)...")

        data = self.load_data("IMU", "IMU2", "VIBE")

        # Deterministic rules
        rule_anomalies = []
        for rule in ALL_VIBRATION_RULES:
            try:
                rule_anomalies.extend(rule.evaluate(data))
            except Exception as e:
                self.log.warning("rule_error", rule=rule.RULE_NAME, error=str(e))

        # FFT analysis
        imu_df = data.get("IMU")
        vibe_df = data.get("VIBE")
        fft_result = None

        if imu_df is not None and not imu_df.is_empty():
            self.emit(state, "Computing FFT / Welch PSD for vibration spectrum...")
            fft_result = analyze_vibration(imu_df, vibe_df)
            self.emit(
                state,
                f"Vibration: Z-RMS={fft_result.z.rms_m_s2:.1f} m/s², "
                f"severity={fft_result.overall_severity}, "
                f"motor_fund={fft_result.identified_motor_fundamental_hz}Hz"
            )

        # Save FFT result to derived storage
        if fft_result:
            self.store.write_derived(self.flight_id, "vibration_fft", fft_result.to_dict())

        # LLM interpretation
        has_issues = rule_anomalies or (fft_result and fft_result.overall_severity in ("WARNING", "CRITICAL"))

        if has_issues:
            evidence = self._build_evidence(rule_anomalies, fft_result)
            result = self.timed_llm_call(
                self.llm.structured,
                messages=[{"role": "user", "content": evidence}],
                response_model=VibrationResult,
                system=VIBRATION_AGENT_PROMPT,
                model=self.llm.fast_model,
            )
            self.emit(state, f"Vibration: {result.overall_severity} — EKF impact likely: {result.ekf_impact_likely}")
        else:
            result = VibrationResult(
                overall_severity="GOOD",
                confidence=0.90,
                summary="Vibration within acceptable parameters. No IMU clipping detected.",
                ekf_impact_likely=False,
                notch_filter_needed=False,
            )
            if fft_result:
                result.rms_x = fft_result.x.rms_m_s2
                result.rms_y = fft_result.y.rms_m_s2
                result.rms_z = fft_result.z.rms_m_s2
                result.motor_fundamental_hz = fft_result.identified_motor_fundamental_hz
            self.emit(state, "VIBRATION: NOMINAL")

        state.setdefault("anomalies", []).extend([
            {"rule_name": a.rule_name, "category": a.category, "severity": a.severity,
             "timestamp_us": a.timestamp_us, "description": a.description,
             "raw_values": a.raw_values, "detected_by": self.AGENT_NAME}
            for a in rule_anomalies
        ])

        state.setdefault("agent_findings", {})[self.AGENT_NAME] = {
            "overall_severity": result.overall_severity,
            "confidence": result.confidence,
            "summary": result.summary,
            "rms_z": result.rms_z,
            "motor_fundamental_hz": result.motor_fundamental_hz,
            "ekf_impact_likely": result.ekf_impact_likely,
            "notch_filter_needed": result.notch_filter_needed,
            "recommended_notch_hz": result.recommended_notch_hz,
            "anomaly_count": len(rule_anomalies),
        }

        return state

    def _build_evidence(self, anomalies, fft_result) -> str:
        lines = ["VIBRATION ANALYSIS EVIDENCE:", ""]
        lines.append("RULE VIOLATIONS:")
        for a in anomalies:
            lines.append(f"  [{a.severity}] {a.rule_name}: {a.description}")
        if not anomalies:
            lines.append("  None")
        if fft_result:
            lines.append("\nFFT ANALYSIS RESULTS:")
            d = fft_result.to_dict()
            for k, v in d.items():
                if k != "peaks_z":
                    lines.append(f"  {k}: {v}")
            if d.get("peaks_z"):
                lines.append("  Frequency peaks (Z axis):")
                for p in d["peaks_z"][:8]:
                    lines.append(f"    {p['hz']:.1f} Hz ({p['db']:.1f} dB) — {p['label']}")
        lines.append("\nProvide VibrationResult based ONLY on this evidence.")
        return "\n".join(lines)
