"""
GPS integrity and spoofing detection rules.
References: RTCA DO-229, ArduPilot GPS failsafe documentation.
"""

import math

import polars as pl

from .base_rule import BaseRule, RuleAnomaly


class GPSHDOPDegradationRule(BaseRule):
    """HDOP > 2.0 sustained WARNING; > 5.0 sustained CRITICAL."""
    RULE_NAME = "GPS_HDOP_DEGRADATION"
    CATEGORY = "GPS"

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        df = data.get("GPS")
        if df is None or df.is_empty() or "hdop" not in df.columns:
            return []

        anomalies = []

        df_warn = df.with_columns((pl.col("hdop") > 2.0).alias("_viol"))
        anomalies += self._find_sustained_violations(
            df_warn, "timestamp_us", "_viol",
            min_duration_us=10_000_000,
            severity="WARNING",
            description_template="GPS HDOP degraded (mean={hdop_mean:.2f}) for {duration_s:.1f}s — reduced positioning accuracy",
            raw_value_cols=["hdop"],
        )

        df_crit = df.with_columns((pl.col("hdop") > 5.0).alias("_viol"))
        anomalies += self._find_sustained_violations(
            df_crit, "timestamp_us", "_viol",
            min_duration_us=5_000_000,
            severity="CRITICAL",
            description_template="GPS HDOP CRITICAL (max={hdop_max:.2f}) for {duration_s:.1f}s — poor geometry, navigation unreliable",
            raw_value_cols=["hdop"],
        )

        return anomalies


class GPSSatCountDropRule(BaseRule):
    """
    Satellite count drop > 4 within 10 seconds.
    Rapid loss of satellites indicates jamming or signal blockage.
    """
    RULE_NAME = "GPS_SAT_COUNT_DROP"
    CATEGORY = "GPS"
    DROP_THRESHOLD = 4
    WINDOW_US = 10_000_000

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        df = data.get("GPS")
        if df is None or df.is_empty() or "num_sats" not in df.columns:
            return []

        anomalies = []
        ts = df["timestamp_us"].to_list()
        sats = df["num_sats"].to_list()

        for i in range(1, len(sats)):
            dt = ts[i] - ts[i - 1]
            if dt <= 0 or dt > self.WINDOW_US:
                continue
            drop = sats[i - 1] - sats[i]
            if drop >= self.DROP_THRESHOLD:
                anomalies.append(RuleAnomaly(
                    rule_name=self.RULE_NAME,
                    category=self.CATEGORY,
                    severity="CRITICAL" if drop >= 6 else "WARNING",
                    timestamp_us=ts[i],
                    end_timestamp_us=None,
                    description=f"GPS satellite count dropped by {drop} ({sats[i-1]}→{sats[i]}) in {dt/1e6:.1f}s — possible jamming or blockage",
                    raw_values={"drop": drop, "before": sats[i-1], "after": sats[i]},
                    correlation_hint="Correlate with GPS HDOP and EKF velocity innovation at same timestamp",
                ))

        return anomalies


class GPSPositionGlitchRule(BaseRule):
    """
    Position jump > 5m between consecutive GPS samples, inconsistent with velocity.
    Classic GPS glitch signature: EKF will reject or generate large innovation.
    """
    RULE_NAME = "GPS_POSITION_GLITCH"
    CATEGORY = "GPS"
    GLITCH_THRESHOLD_M = 5.0
    MAX_EXPECTED_VELOCITY_M_S = 30.0  # above this = probably fixed-wing, don't flag

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        df = data.get("GPS")
        if df is None or df.is_empty():
            return []
        if "lat_deg" not in df.columns or "lng_deg" not in df.columns:
            return []

        # Only compute position jumps between valid 3D-fix samples.
        # When GPS loses fix (fix_type < 3) the receiver reports (0, 0) lat/lng,
        # producing a multi-thousand-km "jump" that is purely an artifact of the
        # no-fix placeholder — not a real navigation glitch.
        if "fix_type" in df.columns:
            df = df.filter(pl.col("fix_type") >= 3)
        if df.is_empty() or len(df) < 2:
            return []

        anomalies = []
        lats = df["lat_deg"].to_list()
        lngs = df["lng_deg"].to_list()
        ts = df["timestamp_us"].to_list()
        spds = df.get_column("gnd_speed_m_s").to_list() if "gnd_speed_m_s" in df.columns else [0.0] * len(lats)

        for i in range(1, len(lats)):
            dt_s = (ts[i] - ts[i - 1]) / 1_000_000.0
            if dt_s <= 0 or dt_s > 5.0:
                continue

            dist_m = _haversine_m(lats[i - 1], lngs[i - 1], lats[i], lngs[i])
            expected_max = max(spds[i], 0.1) * dt_s * 3.0  # 3x speed buffer

            if dist_m > self.GLITCH_THRESHOLD_M and dist_m > expected_max:
                anomalies.append(RuleAnomaly(
                    rule_name=self.RULE_NAME,
                    category=self.CATEGORY,
                    severity="CRITICAL",
                    timestamp_us=ts[i],
                    end_timestamp_us=None,
                    description=f"GPS position jump {dist_m:.1f}m in {dt_s:.2f}s (expected max {expected_max:.1f}m) — GPS GLITCH",
                    raw_values={"jump_m": dist_m, "dt_s": dt_s, "speed_m_s": spds[i]},
                    correlation_hint="Check EKF velocity innovations (NKF3.IVN/IVE) at this timestamp",
                ))

        return anomalies


class GPSLowSatelliteCountRule(BaseRule):
    """Sustained navigation with < 8 satellites is unreliable."""
    RULE_NAME = "GPS_LOW_SAT_COUNT"
    CATEGORY = "GPS"

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        df = data.get("GPS")
        if df is None or df.is_empty() or "num_sats" not in df.columns:
            return []

        df_w = df.with_columns((pl.col("num_sats") < 8).alias("_viol"))
        return self._find_sustained_violations(
            df_w, "timestamp_us", "_viol",
            min_duration_us=15_000_000,
            severity="WARNING",
            description_template="GPS satellite count below 8 (mean={num_sats_mean:.1f}) for {duration_s:.1f}s",
            raw_value_cols=["num_sats"],
        )


class GPSSpoofingIndicatorRule(BaseRule):
    """
    GPS spoofing heuristic indicators:
    - HDOP improves (stronger signal) while satellites appear to drop then recover
    - Position jumps to new location with no velocity transition
    - Position jump WITHOUT corresponding velocity change

    Note: This flags INDICATORS only. Spoofing confirmation requires additional evidence.
    """
    RULE_NAME = "GPS_SPOOF_INDICATOR"
    CATEGORY = "GPS"

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        df = data.get("GPS")
        if df is None or df.is_empty():
            return []

        anomalies = []

        # Indicator: large position jump + HDOP improves simultaneously
        # (a spoofer transmits strong, clean signals — HDOP drops while position jumps)
        if all(c in df.columns for c in ("lat_deg", "lng_deg", "hdop")):
            lats = df["lat_deg"].to_list()
            lngs = df["lng_deg"].to_list()
            ts = df["timestamp_us"].to_list()
            hdops = df["hdop"].to_list()

            for i in range(1, len(lats)):
                dt_s = (ts[i] - ts[i - 1]) / 1_000_000.0
                if dt_s <= 0 or dt_s > 2.0:
                    continue

                dist_m = _haversine_m(lats[i - 1], lngs[i - 1], lats[i], lngs[i])
                hdop_delta = hdops[i - 1] - hdops[i]  # positive = improved

                # Large jump + improved HDOP = spoofing signature
                if dist_m > 10.0 and hdop_delta > 0.3:
                    anomalies.append(RuleAnomaly(
                        rule_name=self.RULE_NAME,
                        category=self.CATEGORY,
                        severity="WARNING",
                        timestamp_us=ts[i],
                        end_timestamp_us=None,
                        description=(
                            f"GPS SPOOFING INDICATOR: position jumped {dist_m:.1f}m "
                            f"while HDOP improved by {hdop_delta:.2f} — strong signal with position jump"
                        ),
                        raw_values={"jump_m": dist_m, "hdop_improvement": hdop_delta},
                        correlation_hint="Cross-check with barometer altitude and INS velocity — spoofing typically does not match baro",
                    ))

        return anomalies


class GPSFixTypeDropRule(BaseRule):
    """GPS fix type drop from 3D to 2D or None during flight."""
    RULE_NAME = "GPS_FIX_DROP"
    CATEGORY = "GPS"

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        df = data.get("GPS")
        if df is None or df.is_empty() or "fix_type" not in df.columns:
            return []

        df_w = df.with_columns((pl.col("fix_type") < 3).alias("_viol"))
        return self._find_sustained_violations(
            df_w, "timestamp_us", "_viol",
            min_duration_us=3_000_000,
            severity="CRITICAL",
            description_template="GPS fix type dropped below 3D (fix={fix_type_mean:.0f}) for {duration_s:.1f}s — no reliable position",
            raw_value_cols=["fix_type"],
        )


ALL_GPS_RULES: list[BaseRule] = [
    GPSHDOPDegradationRule(),
    GPSSatCountDropRule(),
    GPSPositionGlitchRule(),
    GPSLowSatelliteCountRule(),
    GPSSpoofingIndicatorRule(),
    GPSFixTypeDropRule(),
]


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
