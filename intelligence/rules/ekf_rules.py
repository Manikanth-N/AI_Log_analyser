"""
EKF health and innovation diagnostics rules.
References: ArduPilot EKF3 documentation, ardupilot.org/dev/docs/ekf2-navigation-system.html
"""

import polars as pl

from .base_rule import BaseRule, RuleAnomaly, Severity


def _get_nkf4(data: dict[str, pl.DataFrame]) -> pl.DataFrame | None:
    df = data.get("NKF4")
    if df is not None and not df.is_empty():
        return df
    return data.get("XKF4")


def _get_nkf3(data: dict[str, pl.DataFrame]) -> pl.DataFrame | None:
    df = data.get("NKF3")
    if df is not None and not df.is_empty():
        return df
    return data.get("XKF3")


class EKFInnovationVelocityRule(BaseRule):
    """
    Velocity innovation variance ratio > 0.5 sustained = filter stress.
    > 1.0 sustained = filter divergence (GPS rejected, dead-reckoning).
    NKF4.SV (velocity variance ratio).
    """
    RULE_NAME = "EKF_INNOV_VEL"
    CATEGORY = "EKF"

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        df = _get_nkf4(data)
        if df is None or df.is_empty():
            return []

        anomalies = []

        # WARNING: SV > 0.5 for > 5 seconds
        df_warn = df.with_columns(
            (pl.col("var_ratio_vel") > 0.5).alias("_viol")
        )
        anomalies += self._find_sustained_violations(
            df_warn, "timestamp_us", "_viol",
            min_duration_us=5_000_000,
            severity="WARNING",
            description_template="EKF velocity innovation ratio high (mean={var_ratio_vel_mean:.2f}) for {duration_s:.1f}s — GPS/IMU disagreement",
            raw_value_cols=["var_ratio_vel"],
        )

        # CRITICAL: SV > 1.0 for > 2 seconds (filter diverging)
        df_crit = df.with_columns(
            (pl.col("var_ratio_vel") > 1.0).alias("_viol")
        )
        anomalies += self._find_sustained_violations(
            df_crit, "timestamp_us", "_viol",
            min_duration_us=2_000_000,
            severity="CRITICAL",
            description_template="EKF velocity innovation ratio EXCEEDED 1.0 (max={var_ratio_vel_max:.2f}) for {duration_s:.1f}s — FILTER DIVERGENCE",
            raw_value_cols=["var_ratio_vel"],
        )

        return anomalies


class EKFInnovationPositionRule(BaseRule):
    """Position innovation variance ratio (NKF4.SP) > 1.0 = position estimate unreliable."""
    RULE_NAME = "EKF_INNOV_POS"
    CATEGORY = "EKF"

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        df = _get_nkf4(data)
        if df is None or df.is_empty():
            return []

        df = df.with_columns((pl.col("var_ratio_pos") > 1.0).alias("_viol"))
        return self._find_sustained_violations(
            df, "timestamp_us", "_viol",
            min_duration_us=2_000_000,
            severity="CRITICAL",
            description_template="EKF position innovation ratio exceeded 1.0 (max={var_ratio_pos_max:.2f}) for {duration_s:.1f}s",
            raw_value_cols=["var_ratio_pos"],
        )


class EKFLaneSwitchRule(BaseRule):
    """
    EKF lane switch: primary filter lane rejected, switched to secondary.
    ArduPilot ERR messages: Subsys=19 (EKFCHECK) or Subsys=24 (EKF_VARIANCE).
    This is always at minimum WARNING — indicates primary filter failure.
    """
    RULE_NAME = "EKF_LANE_SWITCH"
    CATEGORY = "EKF"

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        anomalies = []

        # Method 1: ERR messages
        err = data.get("ERR")
        if err is not None and not err.is_empty():
            if "subsys" in err.columns and "ecode" in err.columns:
                lane_errs = err.filter(
                    pl.col("subsys").is_in([16, 17, 24])  # EKFCHECK, EKF_VARIANCE
                )
                for row in lane_errs.iter_rows(named=True):
                    anomalies.append(RuleAnomaly(
                        rule_name=self.RULE_NAME,
                        category=self.CATEGORY,
                        severity="CRITICAL",
                        timestamp_us=int(row["timestamp_us"]),
                        end_timestamp_us=None,
                        description=f"EKF error: subsys={row.get('subsys_name', row['subsys'])} ecode={row['ecode']} — filter lane event",
                        raw_values={"subsys": row["subsys"], "ecode": row["ecode"]},
                        correlation_hint="Correlate with GPS HDOP and EKF innovation ratios at this timestamp",
                    ))

        # Method 2: NKF4.FS field changes (lane field)
        nkf4 = data.get("NKF4")
        if nkf4 is not None and not nkf4.is_empty() and "lane" in nkf4.columns:
            lane_changes = nkf4.with_columns(
                pl.col("lane").diff().alias("_lane_diff")
            ).filter(pl.col("_lane_diff").abs() > 0)

            for row in lane_changes.iter_rows(named=True):
                anomalies.append(RuleAnomaly(
                    rule_name=self.RULE_NAME,
                    category=self.CATEGORY,
                    severity="CRITICAL",
                    timestamp_us=int(row["timestamp_us"]),
                    end_timestamp_us=None,
                    description=f"EKF primary lane switched (NKF4.FS changed by {row['_lane_diff']}) — primary filter rejected",
                    raw_values={"lane": row["lane"], "lane_diff": row["_lane_diff"]},
                ))

        return anomalies


class EKFMagneticInnovationRule(BaseRule):
    """
    Heading innovation |NKF3.IYaw| > 0.3 rad sustained = magnetic interference.
    Compass calibration error or ferromagnetic interference.
    """
    RULE_NAME = "EKF_MAG_INNOV"
    CATEGORY = "EKF"

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        df = _get_nkf3(data)
        if df is None or df.is_empty() or "innov_heading" not in df.columns:
            return []

        df = df.with_columns(
            (pl.col("innov_heading").abs() > 0.3).alias("_viol")
        )
        return self._find_sustained_violations(
            df, "timestamp_us", "_viol",
            min_duration_us=5_000_000,
            severity="WARNING",
            description_template="EKF heading innovation high (mean={innov_heading_mean:.3f} rad) for {duration_s:.1f}s — magnetic anomaly",
            raw_value_cols=["innov_heading"],
        )


class EKFHeightInnovationRule(BaseRule):
    """
    NKF4.SH (height variance ratio) > 1.0 = height estimate unreliable.
    Barometer and GPS altitude disagreement.
    """
    RULE_NAME = "EKF_HEIGHT_INNOV"
    CATEGORY = "EKF"

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        df = _get_nkf4(data)
        if df is None or df.is_empty() or "var_ratio_hgt" not in df.columns:
            return []

        df = df.with_columns((pl.col("var_ratio_hgt") > 1.0).alias("_viol"))
        return self._find_sustained_violations(
            df, "timestamp_us", "_viol",
            min_duration_us=3_000_000,
            severity="WARNING",
            description_template="EKF height innovation ratio exceeded 1.0 for {duration_s:.1f}s — baro/GPS altitude disagreement",
            raw_value_cols=["var_ratio_hgt"],
        )


class EKFVelocityInnovationSpikeRule(BaseRule):
    """
    NKF3.IVN or IVE absolute innovation spike > 2 m/s = sudden GPS jump.
    Instantaneous event, often the precursor to lane switch.
    """
    RULE_NAME = "EKF_VEL_INNOV_SPIKE"
    CATEGORY = "EKF"
    SPIKE_THRESHOLD_M_S = 2.0

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        df = _get_nkf3(data)
        if df is None or df.is_empty():
            return []

        anomalies = []
        for col in ("innov_vel_n", "innov_vel_e", "innov_vel_d"):
            if col not in df.columns:
                continue
            spiked = df.with_columns(
                (pl.col(col).abs() > self.SPIKE_THRESHOLD_M_S).alias("_viol")
            )
            anomalies += self._find_instantaneous_violations(
                spiked, "timestamp_us", "_viol",
                severity="CRITICAL",
                description_template=f"EKF velocity innovation spike on {col}: {{{col}:.2f}} m/s — GPS position jump",
                raw_value_cols=[col],
            )
        return anomalies


class EKFPositionOffsetGrowthRule(BaseRule):
    """
    NKF4.OFN/OFE (GPS position correction offsets) growing > 5m.
    Indicates the filter is applying large corrections to GPS, implying GPS error.
    """
    RULE_NAME = "EKF_POS_OFFSET_GROWTH"
    CATEGORY = "EKF"

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        df = _get_nkf4(data)
        if df is None or df.is_empty():
            return []

        anomalies = []
        for col in ("offset_n", "offset_e"):
            if col not in df.columns:
                continue
            large = df.with_columns((pl.col(col).abs() > 5.0).alias("_viol"))
            anomalies += self._find_sustained_violations(
                large, "timestamp_us", "_viol",
                min_duration_us=2_000_000,
                severity="WARNING",
                description_template=f"EKF GPS offset ({col}) > 5m for {{duration_s:.1f}}s — GPS position correction large",
                raw_value_cols=[col],
            )
        return anomalies


ALL_EKF_RULES: list[BaseRule] = [
    EKFInnovationVelocityRule(),
    EKFInnovationPositionRule(),
    EKFLaneSwitchRule(),
    EKFMagneticInnovationRule(),
    EKFHeightInnovationRule(),
    EKFVelocityInnovationSpikeRule(),
    EKFPositionOffsetGrowthRule(),
]
