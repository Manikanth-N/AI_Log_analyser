"""
Vibration and IMU health rules.
Clipping, RMS thresholds, EKF impact assessment.
"""

import polars as pl
import numpy as np

from .base_rule import BaseRule, RuleAnomaly


class IMUClippingRule(BaseRule):
    """
    ArduPilot VIBE.Clip0/1/2: accelerometer clipping events per log interval.
    > 100 clips/second indicates the IMU is saturating — EKF contamination likely.
    ArduPilot documentation: >100 is 'severe', >300 is 'catastrophic'.
    """
    RULE_NAME = "IMU_CLIPPING"
    CATEGORY = "VIBRATION"

    WARN_CLIPS_PER_S = 50
    CRIT_CLIPS_PER_S = 100

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        df = data.get("VIBE")
        if df is None or df.is_empty():
            return []

        clip_cols = [c for c in ("clip0", "clip1", "clip2") if c in df.columns]
        if not clip_cols:
            return []

        # Compute per-sample clip rate (clips per second based on dt)
        ts = df["timestamp_us"].to_numpy()
        dt_s = np.diff(ts) / 1e6
        dt_s = np.concatenate([[dt_s[0]], dt_s])
        dt_s = np.maximum(dt_s, 0.001)

        anomalies = []
        for col in clip_cols:
            clips = df[col].to_numpy().astype(float)
            clip_rate = clips / dt_s

            # WARNING
            warn_mask = clip_rate > self.WARN_CLIPS_PER_S
            crit_mask = clip_rate > self.CRIT_CLIPS_PER_S

            for mask, severity, label in [
                (crit_mask, "CRITICAL", "CATASTROPHIC"),
                (warn_mask & ~crit_mask, "WARNING", "SEVERE"),
            ]:
                indices = np.where(mask)[0]
                if len(indices) == 0:
                    continue
                groups = np.split(indices, np.where(np.diff(indices) > 5)[0] + 1)
                for grp in groups:
                    if len(grp) == 0:
                        continue
                    peak = float(clip_rate[grp].max())
                    anomalies.append(RuleAnomaly(
                        rule_name=self.RULE_NAME,
                        category=self.CATEGORY,
                        severity=severity,
                        timestamp_us=int(ts[grp[0]]),
                        end_timestamp_us=int(ts[grp[-1]]),
                        description=f"IMU {col} clipping rate {peak:.0f}/s ({label}) — accelerometer saturating, EKF contamination risk",
                        raw_values={"clip_rate_per_s": peak, "sensor": col},
                        correlation_hint="Check EKF innovation ratios during this period — clipping corrupts IMU data",
                    ))

        return anomalies


class VibrationRMSRule(BaseRule):
    """
    ArduPilot VIBE.VibeX/Y/Z RMS vibration levels.
    < 5 m/s²: Good. 5–15: Acceptable. 15–30: WARNING. > 30: CRITICAL.
    """
    RULE_NAME = "VIBE_HIGH_RMS"
    CATEGORY = "VIBRATION"

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        df = data.get("VIBE")
        if df is None or df.is_empty():
            return []

        anomalies = []
        for col, axis in [("vibe_x", "X"), ("vibe_y", "Y"), ("vibe_z", "Z")]:
            if col not in df.columns:
                continue

            df_warn = df.with_columns((pl.col(col) > 15.0).alias("_viol"))
            anomalies += self._find_sustained_violations(
                df_warn, "timestamp_us", "_viol",
                min_duration_us=5_000_000,
                severity="WARNING",
                description_template=f"Vibration {axis} RMS high (mean={{{col}_mean:.1f}} m/s²) for {{duration_s:.1f}}s — propeller/motor balance check needed",
                raw_value_cols=[col],
            )

            df_crit = df.with_columns((pl.col(col) > 30.0).alias("_viol"))
            anomalies += self._find_sustained_violations(
                df_crit, "timestamp_us", "_viol",
                min_duration_us=3_000_000,
                severity="CRITICAL",
                description_template=f"Vibration {axis} RMS CRITICAL (max={{{col}_max:.1f}} m/s²) for {{duration_s:.1f}}s — EKF estimation severely degraded",
                raw_value_cols=[col],
            )

        return anomalies


class IMURawClippingRule(BaseRule):
    """
    Direct IMU acceleration magnitude > 29 m/s² = approaching clipping range.
    ArduPilot default IMU range is typically ±16g (157 m/s²), but
    > 30 m/s² total acceleration indicates extreme mechanical shock.
    """
    RULE_NAME = "IMU_RAW_EXTREME"
    CATEGORY = "VIBRATION"

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        df = data.get("IMU")
        if df is None or df.is_empty():
            return []
        if not all(c in df.columns for c in ("acc_x_m_s2", "acc_y_m_s2", "acc_z_m_s2")):
            return []

        df = df.with_columns([
            (
                pl.col("acc_x_m_s2") ** 2 +
                pl.col("acc_y_m_s2") ** 2 +
                pl.col("acc_z_m_s2") ** 2
            ).sqrt().alias("_acc_mag")
        ])

        # Remove gravity (approx 9.81 m/s²) — vibration is excess
        df = df.with_columns(
            (pl.col("_acc_mag") - 9.81).abs().alias("_excess_g")
        )

        df_w = df.with_columns((pl.col("_excess_g") > 20.0).alias("_viol"))
        return self._find_instantaneous_violations(
            df_w, "timestamp_us", "_viol",
            severity="WARNING",
            description_template="IMU excess acceleration {_excess_g:.1f} m/s² above gravity — mechanical shock or severe vibration",
            raw_value_cols=["_excess_g"],
        )


ALL_VIBRATION_RULES: list[BaseRule] = [
    IMUClippingRule(),
    VibrationRMSRule(),
    IMURawClippingRule(),
]
