"""
Motor output, ESC behavior, and thrust asymmetry rules.
"""

import numpy as np
import polars as pl

from .base_rule import BaseRule, RuleAnomaly


class MotorOutputImbalanceRule(BaseRule):
    """
    During stable hover (|roll| < 5°, |pitch| < 5°), motor outputs should be
    within 15% of each other (CV < 0.15). Persistent imbalance = prop/motor damage.
    """
    RULE_NAME = "MOTOR_IMBALANCE"
    CATEGORY = "MOTOR"
    CV_WARN = 0.10
    CV_CRIT = 0.20

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        rcou = data.get("RCOU")
        att = data.get("ATT")
        if rcou is None or att is None or rcou.is_empty() or att.is_empty():
            return []

        motor_cols = [c for c in rcou.columns if c in ("ch1_us", "ch2_us", "ch3_us", "ch4_us")]
        if len(motor_cols) < 4:
            return []

        # Align timestamps: join ATT to RCOU by nearest timestamp
        rcou_ts = rcou["timestamp_us"].to_numpy()
        att_ts = att["timestamp_us"].to_numpy()
        att_roll = att["roll_deg"].to_numpy() if "roll_deg" in att.columns else None
        att_pitch = att["pitch_deg"].to_numpy() if "pitch_deg" in att.columns else None

        if att_roll is None or att_pitch is None:
            return []

        # Interpolate attitude onto RCOU timestamps
        roll_interp = np.interp(rcou_ts, att_ts, att_roll)
        pitch_interp = np.interp(rcou_ts, att_ts, att_pitch)

        hover_mask = (np.abs(roll_interp) < 5.0) & (np.abs(pitch_interp) < 5.0)

        # Need at least 30 seconds of hover data
        hover_indices = np.where(hover_mask)[0]
        if len(hover_indices) < 100:
            return []

        hover_rcou = rcou.filter(pl.Series(hover_mask))
        motor_means = [float(hover_rcou[col].mean()) for col in motor_cols]
        overall_mean = np.mean(motor_means)

        if overall_mean < 100:  # unreasonably low, probably not valid
            return []

        cv = float(np.std(motor_means) / overall_mean)

        if cv > self.CV_WARN:
            highest = motor_cols[int(np.argmax(motor_means))]
            lowest = motor_cols[int(np.argmin(motor_means))]
            severity = "CRITICAL" if cv > self.CV_CRIT else "WARNING"
            ts = int(rcou["timestamp_us"][0])
            return [RuleAnomaly(
                rule_name=self.RULE_NAME,
                category=self.CATEGORY,
                severity=severity,
                timestamp_us=ts,
                end_timestamp_us=None,
                description=(
                    f"Motor output imbalance during hover: CV={cv:.3f} "
                    f"(threshold {self.CV_WARN}). "
                    f"Highest={highest} ({motor_means[motor_cols.index(highest)]:.0f}µs), "
                    f"Lowest={lowest} ({motor_means[motor_cols.index(lowest)]:.0f}µs). "
                    f"Propeller imbalance or motor calibration issue."
                ),
                raw_values={"cv": cv, "means": dict(zip(motor_cols, motor_means))},
                correlation_hint="Check vibration FFT for 1P frequency matching motor RPM — prop imbalance generates 1P vibration",
            )]

        return []


class ESCDesyncRule(BaseRule):
    """
    ESC desync: motor RPM drops > 30% within 100ms while command stays high.
    Requires ESC telemetry (BLHeli passthrough or ArduPilot ESC logging).
    """
    RULE_NAME = "ESC_DESYNC"
    CATEGORY = "MOTOR"
    RPM_DROP_THRESHOLD = 0.30
    WINDOW_US = 100_000

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        esc = data.get("ESC")
        if esc is None or esc.is_empty() or "rpm" not in esc.columns:
            return []

        ts = esc["timestamp_us"].to_numpy()
        rpm = esc["rpm"].to_numpy()

        if len(rpm) < 5:
            return []

        anomalies = []
        for i in range(1, len(rpm)):
            dt = ts[i] - ts[i - 1]
            if dt <= 0 or dt > self.WINDOW_US * 2:
                continue
            if rpm[i - 1] < 500:  # not spinning
                continue
            drop_pct = (rpm[i - 1] - rpm[i]) / max(rpm[i - 1], 1)
            if drop_pct > self.RPM_DROP_THRESHOLD:
                instance = int(esc["instance"][i]) if "instance" in esc.columns else -1
                anomalies.append(RuleAnomaly(
                    rule_name=self.RULE_NAME,
                    category=self.CATEGORY,
                    severity="CRITICAL",
                    timestamp_us=int(ts[i]),
                    end_timestamp_us=None,
                    description=f"ESC DESYNC: Motor {instance} RPM dropped {drop_pct*100:.0f}% ({rpm[i-1]:.0f}→{rpm[i]:.0f} RPM) in {dt/1000:.0f}ms",
                    raw_values={"rpm_before": rpm[i - 1], "rpm_after": rpm[i], "drop_pct": drop_pct, "motor": instance},
                    correlation_hint="Check voltage sag and current spike on BAT at this timestamp — ESC desync often causes current transient",
                ))

        return anomalies


class MotorOutputSaturationRule(BaseRule):
    """
    Any motor output at maximum (> 1950µs for PWM) sustained > 2 seconds.
    Indicates loss of control authority or vehicle near crash.
    """
    RULE_NAME = "MOTOR_OUTPUT_SATURATION"
    CATEGORY = "MOTOR"
    SAT_THRESHOLD_US = 1950

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        rcou = data.get("RCOU")
        if rcou is None or rcou.is_empty():
            return []

        motor_cols = [c for c in rcou.columns if c in ("ch1_us", "ch2_us", "ch3_us", "ch4_us")]
        anomalies = []

        for col in motor_cols:
            df_w = rcou.with_columns((pl.col(col) > self.SAT_THRESHOLD_US).alias("_viol"))
            anomalies += self._find_sustained_violations(
                df_w, "timestamp_us", "_viol",
                min_duration_us=2_000_000,
                severity="CRITICAL",
                description_template=f"Motor {col} at saturation (>{self.SAT_THRESHOLD_US}µs) for {{duration_s:.1f}}s — loss of control authority",
                raw_value_cols=[col],
            )

        return anomalies


class MotorOutputDropRule(BaseRule):
    """
    One motor drops to minimum (< 1100µs) while others remain high.
    Classic ESC failure or motor stop signature.
    """
    RULE_NAME = "MOTOR_OUTPUT_DROP"
    CATEGORY = "MOTOR"
    MIN_THRESHOLD_US = 1100
    HIGH_THRESHOLD_US = 1400

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        rcou = data.get("RCOU")
        if rcou is None or rcou.is_empty():
            return []

        motor_cols = [c for c in rcou.columns if c in ("ch1_us", "ch2_us", "ch3_us", "ch4_us")]
        if len(motor_cols) < 2:
            return []

        anomalies = []
        ts = rcou["timestamp_us"].to_list()
        motor_data = {col: rcou[col].to_list() for col in motor_cols}

        for i in range(len(ts)):
            outputs = [motor_data[col][i] for col in motor_cols]
            low_motors = [col for col, v in zip(motor_cols, outputs) if v < self.MIN_THRESHOLD_US]
            high_motors = [col for col, v in zip(motor_cols, outputs) if v > self.HIGH_THRESHOLD_US]

            if low_motors and high_motors:
                anomalies.append(RuleAnomaly(
                    rule_name=self.RULE_NAME,
                    category=self.CATEGORY,
                    severity="CRITICAL",
                    timestamp_us=int(ts[i]),
                    end_timestamp_us=None,
                    description=(
                        f"MOTOR OUTPUT ASYMMETRY: {low_motors} at minimum (<{self.MIN_THRESHOLD_US}µs) "
                        f"while {high_motors} remain high — motor stop or ESC failure"
                    ),
                    raw_values={col: v for col, v in zip(motor_cols, outputs)},
                    correlation_hint="If armed, this indicates complete motor failure on low channels. Check ESC desync events.",
                ))

        # Deduplicate close events (keep one per 5-second window)
        deduped = []
        last_ts = -5_000_000
        for a in anomalies:
            if a.timestamp_us - last_ts > 5_000_000:
                deduped.append(a)
                last_ts = a.timestamp_us

        return deduped


ALL_MOTOR_RULES: list[BaseRule] = [
    MotorOutputImbalanceRule(),
    ESCDesyncRule(),
    MotorOutputSaturationRule(),
    MotorOutputDropRule(),
]
