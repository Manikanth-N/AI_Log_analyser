"""Unit tests for EKF rules."""

import polars as pl
import pytest

from intelligence.rules.ekf_rules import (
    EKFInnovationVelocityRule,
    EKFPositionOffsetGrowthRule,
)


def _nkf4(var_ratio_vel: list[float], offset_n: list[float] | None = None,
          offset_e: list[float] | None = None) -> pl.DataFrame:
    n = len(var_ratio_vel)
    d = {
        "timestamp_us": [i * 100_000 for i in range(n)],
        "var_ratio_vel": var_ratio_vel,
        "var_ratio_pos": [0.1] * n,
    }
    if offset_n is not None:
        d["offset_n"] = offset_n
        d["offset_e"] = offset_e or [0.0] * n
    return pl.DataFrame(d)


class TestEKFInnovationVelocityRule:
    rule = EKFInnovationVelocityRule()

    def test_nominal_no_anomalies(self):
        # 60 samples × 100ms = 6s of nominal data
        data = {"NKF4": _nkf4([0.2] * 60)}
        assert self.rule.evaluate(data) == []

    def test_warning_above_0_5_sustained(self):
        # 60 samples × 100ms = 6s > 5s min_duration → WARNING
        data = {"NKF4": _nkf4([0.7] * 60)}
        anomalies = self.rule.evaluate(data)
        assert any(a.severity == "WARNING" for a in anomalies)

    def test_critical_above_1_0_sustained(self):
        # 30 samples × 100ms = 3s > 2s min_duration → CRITICAL
        data = {"NKF4": _nkf4([1.5] * 30)}
        anomalies = self.rule.evaluate(data)
        assert any(a.severity == "CRITICAL" for a in anomalies)

    def test_missing_nkf4_returns_empty(self):
        assert self.rule.evaluate({}) == []

    def test_xkf4_fallback(self):
        # Should also work with XKF4 (PX4)
        data = {"XKF4": _nkf4([1.5] * 30)}
        anomalies = self.rule.evaluate(data)
        assert any(a.severity == "CRITICAL" for a in anomalies)

    def test_brief_spike_no_critical(self):
        vals = [0.1] * 60
        vals[10] = 1.5  # single spike, not sustained
        data = {"NKF4": _nkf4(vals)}
        anomalies = self.rule.evaluate(data)
        assert not any(a.severity == "CRITICAL" for a in anomalies)


class TestEKFPositionOffsetGrowthRule:
    rule = EKFPositionOffsetGrowthRule()

    def test_no_anomaly_small_offset(self):
        # offset < 5m, no anomaly
        n = 30
        data = {"NKF4": _nkf4([0.1] * n, offset_n=[2.0] * n, offset_e=[1.0] * n)}
        assert self.rule.evaluate(data) == []

    def test_anomaly_large_offset_sustained(self):
        # offset > 5m for 30 samples × 100ms = 3s > 2s threshold
        n = 30
        data = {"NKF4": _nkf4([0.1] * n, offset_n=[8.0] * n, offset_e=[0.0] * n)}
        anomalies = self.rule.evaluate(data)
        assert len(anomalies) > 0

    def test_missing_offset_col_returns_empty(self):
        # NKF4 present but no offset columns
        data = {"NKF4": _nkf4([0.1] * 10)}
        assert self.rule.evaluate(data) == []

    def test_missing_nkf4_returns_empty(self):
        assert self.rule.evaluate({}) == []
