"""
Power rail and battery forensics rules.
Brownout, sag, current spikes, cell imbalance detection.
"""

import numpy as np
import polars as pl

from .base_rule import BaseRule, RuleAnomaly


class BatteryVoltageSagRule(BaseRule):
    """
    Voltage drop rate > 1 V/s under load = battery sag.
    Drop rate > 3 V/s = brownout risk.
    Computed as rolling dV/dt.
    """
    RULE_NAME = "BAT_VOLTAGE_SAG"
    CATEGORY = "POWER"

    SAG_WARN_V_PER_S = 1.0
    SAG_CRIT_V_PER_S = 3.0

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        _df = data.get("BAT"); df = _df if (_df is not None and not _df.is_empty()) else data.get("CURR")
        if df is None or df.is_empty() or "voltage_v" not in df.columns:
            return []

        volt = df["voltage_v"].to_numpy()
        ts_us = df["timestamp_us"].to_numpy()
        ts_s = ts_us * 1e-6

        if len(volt) < 3:
            return []

        dv_dt = np.gradient(volt, ts_s)  # V/s (negative = dropping)

        anomalies = []

        # WARNING: rapid drop
        warn_mask = dv_dt < -self.SAG_WARN_V_PER_S
        anomalies += self._mask_to_anomalies(ts_us, dv_dt, warn_mask, "WARNING")

        # CRITICAL: severe drop (brownout risk)
        crit_mask = dv_dt < -self.SAG_CRIT_V_PER_S
        anomalies += self._mask_to_anomalies(ts_us, dv_dt, crit_mask, "CRITICAL")

        return anomalies

    def _mask_to_anomalies(self, ts_us, dv_dt, mask, severity) -> list[RuleAnomaly]:
        anomalies = []
        indices = np.where(mask)[0]
        if len(indices) == 0:
            return anomalies
        # Group consecutive indices
        groups = np.split(indices, np.where(np.diff(indices) > 3)[0] + 1)
        for grp in groups:
            if len(grp) == 0:
                continue
            worst = float(dv_dt[grp].min())
            anomalies.append(RuleAnomaly(
                rule_name=self.RULE_NAME,
                category=self.CATEGORY,
                severity=severity,
                timestamp_us=int(ts_us[grp[0]]),
                end_timestamp_us=int(ts_us[grp[-1]]),
                description=f"Battery voltage sag rate {worst:.2f} V/s — {'brownout risk' if severity == 'CRITICAL' else 'voltage stress'}",
                raw_values={"dv_dt_worst": worst},
                correlation_hint="Correlate with RCOU throttle commands and ESC current draw",
            ))
        return anomalies


class BrownoutSignatureRule(BaseRule):
    """
    Brownout signature: voltage < 3.3V per cell under any load.
    For 4S: < 13.2V. Uses cell_count if available, else infers from voltage.
    """
    RULE_NAME = "BAT_BROWNOUT"
    CATEGORY = "POWER"
    MIN_CELL_V = 3.3

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        _df = data.get("BAT"); df = _df if (_df is not None and not _df.is_empty()) else data.get("CURR")
        if df is None or df.is_empty() or "voltage_v" not in df.columns:
            return []

        volt = df["voltage_v"].to_numpy()
        # Infer cell count from max voltage (assumes fully charged = 4.2V/cell)
        max_v = float(np.max(volt))
        inferred_cells = max(1, round(max_v / 4.2))
        brownout_threshold = self.MIN_CELL_V * inferred_cells

        df_w = df.with_columns(
            (pl.col("voltage_v") < brownout_threshold).alias("_viol")
        )
        results = self._find_sustained_violations(
            df_w, "timestamp_us", "_viol",
            min_duration_us=500_000,  # 0.5s is enough for brownout concern
            severity="CRITICAL",
            description_template=f"BROWNOUT SIGNATURE: voltage below {brownout_threshold:.1f}V ({inferred_cells}S × {self.MIN_CELL_V}V) for {{duration_s:.1f}}s",
            raw_value_cols=["voltage_v"],
        )
        for r in results:
            r.correlation_hint = "Check for ESC resets, EKF anomalies, or motor output drops at this timestamp"
        return results


class CurrentSpikeRule(BaseRule):
    """
    Current draw > 2.5× rolling mean = current spike.
    Indicates motor jam, ESC fault, or short circuit.
    """
    RULE_NAME = "BAT_CURRENT_SPIKE"
    CATEGORY = "POWER"
    SPIKE_MULTIPLIER = 2.5
    ROLLING_WINDOW = 20

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        _df = data.get("BAT"); df = _df if (_df is not None and not _df.is_empty()) else data.get("CURR")
        if df is None or df.is_empty() or "current_a" not in df.columns:
            return []

        curr = df["current_a"].to_numpy()
        ts_us = df["timestamp_us"].to_numpy()

        if len(curr) < self.ROLLING_WINDOW * 2:
            return []

        rolling_mean = np.convolve(curr, np.ones(self.ROLLING_WINDOW) / self.ROLLING_WINDOW, mode="same")
        rolling_mean = np.maximum(rolling_mean, 1.0)  # avoid division by near-zero

        spike_mask = curr > rolling_mean * self.SPIKE_MULTIPLIER

        anomalies = []
        indices = np.where(spike_mask)[0]
        groups = np.split(indices, np.where(np.diff(indices) > 5)[0] + 1) if len(indices) > 0 else []
        for grp in groups:
            if len(grp) == 0:
                continue
            peak = float(curr[grp].max())
            mean_at = float(rolling_mean[grp[0]])
            anomalies.append(RuleAnomaly(
                rule_name=self.RULE_NAME,
                category=self.CATEGORY,
                severity="WARNING",
                timestamp_us=int(ts_us[grp[0]]),
                end_timestamp_us=int(ts_us[grp[-1]]),
                description=f"Current spike {peak:.1f}A ({peak/mean_at:.1f}× baseline {mean_at:.1f}A) — possible motor jam or ESC fault",
                raw_values={"peak_a": peak, "baseline_a": mean_at},
                correlation_hint="Check RCOU motor outputs and ESC telemetry at this timestamp",
            ))

        return anomalies


class LowBatteryCapacityRule(BaseRule):
    """Battery remaining < 20% is WARNING; < 10% is CRITICAL."""
    RULE_NAME = "BAT_LOW_CAPACITY"
    CATEGORY = "POWER"

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        df = data.get("BAT")
        if df is None or df.is_empty() or "remaining_pct" not in df.columns:
            return []

        anomalies = []

        df_warn = df.with_columns((pl.col("remaining_pct") < 20.0).alias("_viol"))
        anomalies += self._find_sustained_violations(
            df_warn, "timestamp_us", "_viol",
            min_duration_us=5_000_000,
            severity="WARNING",
            description_template="Battery below 20% (mean={remaining_pct_mean:.1f}%) for {duration_s:.1f}s",
            raw_value_cols=["remaining_pct"],
        )

        df_crit = df.with_columns((pl.col("remaining_pct") < 10.0).alias("_viol"))
        anomalies += self._find_sustained_violations(
            df_crit, "timestamp_us", "_viol",
            min_duration_us=2_000_000,
            severity="CRITICAL",
            description_template="Battery CRITICALLY LOW (mean={remaining_pct_mean:.1f}%) for {duration_s:.1f}s — imminent failsafe",
            raw_value_cols=["remaining_pct"],
        )

        return anomalies


class HighInternalResistanceRule(BaseRule):
    """
    Estimate battery internal resistance from voltage sag under load.
    R_int = ΔV / ΔI. High R_int = degraded or cold cells.
    """
    RULE_NAME = "BAT_HIGH_R_INT"
    CATEGORY = "POWER"
    R_INT_WARN_OHM = 0.08
    MIN_CURRENT_DELTA = 5.0  # Need at least 5A swing for valid estimate

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        _df = data.get("BAT"); df = _df if (_df is not None and not _df.is_empty()) else data.get("CURR")
        if df is None or df.is_empty():
            return []
        if "voltage_v" not in df.columns or "current_a" not in df.columns:
            return []

        volt = df["voltage_v"].to_numpy()
        curr = df["current_a"].to_numpy()

        if len(volt) < 10:
            return []

        dv = np.diff(volt)
        di = np.diff(curr)

        valid = np.abs(di) > self.MIN_CURRENT_DELTA
        if not np.any(valid):
            return []

        r_estimates = -dv[valid] / di[valid]
        r_estimates = r_estimates[(r_estimates > 0) & (r_estimates < 1.0)]

        if len(r_estimates) == 0:
            return []

        r_median = float(np.median(r_estimates))

        if r_median > self.R_INT_WARN_OHM:
            ts_us = int(df["timestamp_us"][0])
            return [RuleAnomaly(
                rule_name=self.RULE_NAME,
                category=self.CATEGORY,
                severity="WARNING" if r_median < 0.15 else "CRITICAL",
                timestamp_us=ts_us,
                end_timestamp_us=None,
                description=f"Battery internal resistance estimated at {r_median*1000:.0f} mΩ (threshold {self.R_INT_WARN_OHM*1000:.0f} mΩ) — degraded cells",
                raw_values={"r_int_ohm": r_median},
            )]

        return []


ALL_POWER_RULES: list[BaseRule] = [
    BatteryVoltageSagRule(),
    BrownoutSignatureRule(),
    CurrentSpikeRule(),
    LowBatteryCapacityRule(),
    HighInternalResistanceRule(),
]
