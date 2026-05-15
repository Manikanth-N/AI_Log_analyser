"""Unit tests for power system rules."""

import polars as pl
import pytest

from intelligence.rules.power_rules import (
    BatteryVoltageSagRule,
    BrownoutSignatureRule,
    LowBatteryCapacityRule,
)


def _bat_df(voltage: list[float], current: list[float] | None = None,
            remaining: list[float] | None = None) -> pl.DataFrame:
    n = len(voltage)
    return pl.DataFrame({
        "timestamp_us": [i * 200_000 for i in range(n)],
        "voltage_v": voltage,
        "current_a": current or [10.0] * n,
        "remaining_pct": remaining or [80.0] * n,
        "consumed_mah": [i * 10.0 for i in range(n)],
    })


class TestBatteryVoltageSagRule:
    rule = BatteryVoltageSagRule()

    def test_stable_voltage_no_anomaly(self):
        v = [16.8] * 20
        assert self.rule.evaluate({"BAT": _bat_df(v)}) == []

    def test_slow_discharge_no_anomaly(self):
        # Normal linear discharge — dV/dt well within threshold
        v = [16.8 - i * 0.001 for i in range(20)]
        assert self.rule.evaluate({"BAT": _bat_df(v)}) == []

    def test_rapid_sag_triggers(self):
        # Sharp voltage collapse: drop 4V over 0.2s = -20 V/s >> 1 V/s threshold
        v = [16.8] * 10 + [12.8] * 10
        anomalies = self.rule.evaluate({"BAT": _bat_df(v)})
        assert len(anomalies) > 0

    def test_missing_bat_returns_empty(self):
        assert self.rule.evaluate({}) == []

    def test_curr_fallback(self):
        # CURR key should work as fallback when BAT absent
        v = [16.8] * 10 + [12.8] * 10
        anomalies = self.rule.evaluate({"CURR": _bat_df(v)})
        assert len(anomalies) > 0


class TestBrownoutSignatureRule:
    rule = BrownoutSignatureRule()

    def test_healthy_4s_voltage_no_brownout(self):
        # 4S at 15.2V = 3.8V/cell, well above 3.3V
        v = [16.8] + [15.2] * 19  # first point establishes cell count
        assert self.rule.evaluate({"BAT": _bat_df(v)}) == []

    def test_brownout_below_threshold(self):
        # 4S (inferred from max 16.8V), critical = 4 × 3.3 = 13.2V
        # Give 10 samples at 200ms each = 2s > 0.5s threshold
        v = [16.8] + [12.8] * 10 + [15.0] * 9
        anomalies = self.rule.evaluate({"BAT": _bat_df(v)})
        assert any(a.rule_name == "BAT_BROWNOUT" for a in anomalies)


class TestLowBatteryCapacityRule:
    rule = LowBatteryCapacityRule()

    def test_healthy_remaining_no_anomaly(self):
        anomalies = self.rule.evaluate({"BAT": _bat_df([16.8] * 10, remaining=[60.0] * 10)})
        assert anomalies == []

    def test_warning_below_20_pct_sustained(self):
        # 30 samples × 200ms = 6s > 5s min_duration
        anomalies = self.rule.evaluate({"BAT": _bat_df([14.0] * 30, remaining=[15.0] * 30)})
        assert any(a.severity == "WARNING" for a in anomalies)

    def test_critical_below_10_pct_sustained(self):
        # 15 samples × 200ms = 3s > 2s min_duration
        anomalies = self.rule.evaluate({"BAT": _bat_df([13.5] * 15, remaining=[8.0] * 15)})
        assert any(a.severity == "CRITICAL" for a in anomalies)
