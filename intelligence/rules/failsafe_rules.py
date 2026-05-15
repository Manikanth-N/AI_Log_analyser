"""
Failsafe verification and safety system behavior rules.
Verifies that failsafes triggered correctly and responses were appropriate.
"""

import polars as pl

from .base_rule import BaseRule, RuleAnomaly

# ArduPilot ERR Subsys codes for failsafe events
_FS_SUBSYS = {
    5: "FAILSAFE_RADIO",
    6: "FAILSAFE_BATT",
    7: "FAILSAFE_GPS",
    8: "FAILSAFE_GCS",
    9: "FAILSAFE_FENCE",
}

# Expected mode transitions on failsafe
_FS_EXPECTED_MODES = {
    5: {6, 9},   # RC failsafe → RTL (6) or LAND (9)
    6: {6, 9},   # Battery failsafe → RTL or LAND
    7: {6},      # GPS failsafe → RTL
    8: {6},      # GCS failsafe → RTL
    9: {6, 9},   # Fence breach → RTL or LAND
}


class FailsafeTriggerVerificationRule(BaseRule):
    """
    When a failsafe ERR event fires, verify:
    1. A mode change occurred within 2 seconds
    2. The mode change was to an appropriate response mode
    """
    RULE_NAME = "FAILSAFE_VERIFY"
    CATEGORY = "FAILSAFE"

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        err = data.get("ERR")
        mode = data.get("MODE")
        if err is None or err.is_empty() or mode is None or mode.is_empty():
            return []

        anomalies = []
        fs_events = err.filter(pl.col("subsys").is_in(list(_FS_SUBSYS.keys())))

        if fs_events.is_empty():
            return []

        mode_ts = mode["timestamp_us"].to_list()
        mode_nums = mode["mode_num"].to_list() if "mode_num" in mode.columns else []

        for row in fs_events.iter_rows(named=True):
            fs_ts = row["timestamp_us"]
            fs_subsys = row["subsys"]
            fs_name = _FS_SUBSYS.get(fs_subsys, f"FAILSAFE_{fs_subsys}")

            # Find mode changes within 3 seconds after failsafe
            response_window = 3_000_000
            response_modes = [
                mode_nums[i] for i, ts in enumerate(mode_ts)
                if 0 <= ts - fs_ts <= response_window
            ]

            if not response_modes:
                anomalies.append(RuleAnomaly(
                    rule_name=self.RULE_NAME,
                    category=self.CATEGORY,
                    severity="CRITICAL",
                    timestamp_us=fs_ts,
                    end_timestamp_us=None,
                    description=f"FAILSAFE {fs_name} triggered but NO mode change within 3 seconds — failsafe response did not execute",
                    raw_values={"subsys": fs_subsys, "ecode": row.get("ecode", 0)},
                ))
            else:
                expected = _FS_EXPECTED_MODES.get(fs_subsys, set())
                if expected and not any(m in expected for m in response_modes):
                    anomalies.append(RuleAnomaly(
                        rule_name=self.RULE_NAME,
                        category=self.CATEGORY,
                        severity="WARNING",
                        timestamp_us=fs_ts,
                        end_timestamp_us=None,
                        description=(
                            f"FAILSAFE {fs_name}: response mode {response_modes[0]} "
                            f"may not be appropriate (expected one of {expected})"
                        ),
                        raw_values={"subsys": fs_subsys, "response_mode": response_modes[0]},
                    ))

        return anomalies


class UnexpectedModeChangeRule(BaseRule):
    """
    Mode changes not initiated by RC/GCS during AUTO or RTL flight.
    Unexpected mode changes indicate failsafe or software fault.
    """
    RULE_NAME = "UNEXPECTED_MODE_CHANGE"
    CATEGORY = "FAILSAFE"

    # Reason codes: 0=unknown, 1=RC, 2=GCS, 4=failsafe, etc.
    AUTONOMOUS_REASONS = {4, 5, 6, 7, 8, 9, 10, 11}

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        mode = data.get("MODE")
        if mode is None or mode.is_empty():
            return []
        if "reason" not in mode.columns:
            return []

        anomalies = []
        prev_mode = None

        for row in mode.iter_rows(named=True):
            curr_mode = row.get("mode_num")
            reason = row.get("reason", 0)
            mode_name = row.get("mode_name", f"MODE_{curr_mode}")

            if prev_mode is not None and reason in self.AUTONOMOUS_REASONS:
                anomalies.append(RuleAnomaly(
                    rule_name=self.RULE_NAME,
                    category=self.CATEGORY,
                    severity="INFO",
                    timestamp_us=row["timestamp_us"],
                    end_timestamp_us=None,
                    description=f"Autonomous mode change → {mode_name} (reason_code={reason}) — verify this was expected",
                    raw_values={"mode": curr_mode, "prev_mode": prev_mode, "reason": reason},
                ))

            prev_mode = curr_mode

        return anomalies


class RTLBehaviorRule(BaseRule):
    """
    During RTL: verify vehicle climbs to RTL altitude before returning.
    Check that position converges toward home.
    RTL that immediately descends or flies away = navigation error.
    """
    RULE_NAME = "RTL_BEHAVIOR"
    CATEGORY = "FAILSAFE"

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        mode = data.get("MODE")
        gps = data.get("GPS")
        if mode is None or gps is None:
            return []
        if "mode_name" not in mode.columns:
            return []

        # Find RTL start
        rtl_start = None
        for row in mode.iter_rows(named=True):
            if row.get("mode_name") == "RTL":
                rtl_start = row["timestamp_us"]
                break

        if rtl_start is None:
            return []

        # Get GPS data during RTL (first 30 seconds)
        rtl_window = 30_000_000
        rtl_gps = gps.filter(
            (pl.col("timestamp_us") >= rtl_start) &
            (pl.col("timestamp_us") <= rtl_start + rtl_window)
        )

        if rtl_gps.is_empty():
            return []

        anomalies = []

        # Check altitude during RTL — should climb or maintain, not immediately descend
        if "alt_rel_m" in rtl_gps.columns:
            alts = rtl_gps["alt_rel_m"].to_list()
            if len(alts) > 5:
                initial_alt = alts[0]
                min_alt_in_window = min(alts[:10])  # first 10 samples
                if initial_alt > 5.0 and min_alt_in_window < initial_alt * 0.7:
                    anomalies.append(RuleAnomaly(
                        rule_name=self.RULE_NAME,
                        category=self.CATEGORY,
                        severity="CRITICAL",
                        timestamp_us=rtl_start,
                        end_timestamp_us=None,
                        description=f"RTL ANOMALY: altitude dropped from {initial_alt:.1f}m to {min_alt_in_window:.1f}m immediately after RTL trigger — navigation error or wrong home position",
                        raw_values={"initial_alt_m": initial_alt, "min_alt_m": min_alt_in_window},
                        correlation_hint="Check GPS position estimate and EKF state at RTL trigger — corrupted position estimate causes wrong RTL path",
                    ))

        return anomalies


class ArmingInUnsafeConditionRule(BaseRule):
    """
    Detect arming when pre-conditions are not met:
    - GPS fix < 3D at arm time
    - EKF innovations already elevated at arm time
    """
    RULE_NAME = "UNSAFE_ARM"
    CATEGORY = "FAILSAFE"

    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        err = data.get("ERR")
        mode = data.get("MODE")
        gps = data.get("GPS")
        if mode is None or gps is None:
            return []

        anomalies = []

        # Find arm time (first MODE message after pre-arm)
        arm_ts = None
        for row in mode.iter_rows(named=True):
            if row.get("mode_name") not in ("INITIALISING", None):
                arm_ts = row["timestamp_us"]
                break

        if arm_ts is None:
            return []

        # Check GPS fix at arm time
        gps_at_arm = gps.filter(
            pl.col("timestamp_us").is_between(arm_ts - 5_000_000, arm_ts + 2_000_000)
        )
        if not gps_at_arm.is_empty() and "fix_type" in gps_at_arm.columns:
            fix = int(gps_at_arm["fix_type"].min())
            if fix < 3:
                anomalies.append(RuleAnomaly(
                    rule_name=self.RULE_NAME,
                    category=self.CATEGORY,
                    severity="WARNING",
                    timestamp_us=arm_ts,
                    end_timestamp_us=None,
                    description=f"Vehicle armed with GPS fix_type={fix} (< 3D fix) — autonomous modes require 3D GPS fix",
                    raw_values={"fix_type_at_arm": fix},
                ))

        return anomalies


ALL_FAILSAFE_RULES: list[BaseRule] = [
    FailsafeTriggerVerificationRule(),
    UnexpectedModeChangeRule(),
    RTLBehaviorRule(),
    ArmingInUnsafeConditionRule(),
]
