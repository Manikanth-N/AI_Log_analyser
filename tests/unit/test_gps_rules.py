"""Unit tests for GPS rules."""

import polars as pl
import pytest

from intelligence.rules.gps_rules import (
    GPSHDOPDegradationRule,
    GPSPositionGlitchRule,
    GPSSatCountDropRule,
)


def _gps_df(hdop: list[float] | None = None,
            num_sats: list[int] | None = None,
            lat: list[float] | None = None,
            lng: list[float] | None = None,
            fix_type: list[int] | None = None,
            gnd_speed: list[float] | None = None) -> pl.DataFrame:
    n = max(len(hdop or []), len(num_sats or []), len(lat or []), 10)
    return pl.DataFrame({
        "timestamp_us": [i * 200_000 for i in range(n)],
        "hdop": hdop or [1.2] * n,
        "num_sats": num_sats or [14] * n,
        "lat_deg": lat or [37.7749] * n,
        "lng_deg": lng or [-122.4194] * n,
        "fix_type": fix_type or [3] * n,
        "gnd_speed_m_s": gnd_speed or [0.0] * n,
    })


class TestGPSHDOPDegradationRule:
    rule = GPSHDOPDegradationRule()

    def test_good_hdop_no_anomaly(self):
        assert self.rule.evaluate({"GPS": _gps_df(hdop=[1.0] * 10)}) == []

    def test_high_hdop_warning_sustained(self):
        # 60 samples × 200ms = 12s > 10s min_duration
        anomalies = self.rule.evaluate({"GPS": _gps_df(hdop=[3.0] * 60)})
        assert any(a.severity == "WARNING" for a in anomalies)

    def test_missing_gps_returns_empty(self):
        assert self.rule.evaluate({}) == []


class TestGPSSatCountDropRule:
    rule = GPSSatCountDropRule()

    def test_stable_sats_no_anomaly(self):
        assert self.rule.evaluate({"GPS": _gps_df(num_sats=[14] * 20)}) == []

    def test_large_drop_triggers(self):
        # Drop of 6 sats in one step → CRITICAL
        counts = [14] * 10 + [8] * 10
        anomalies = self.rule.evaluate({"GPS": _gps_df(num_sats=counts)})
        assert len(anomalies) > 0

    def test_missing_gps_returns_empty(self):
        assert self.rule.evaluate({}) == []


class TestGPSPositionGlitchRule:
    rule = GPSPositionGlitchRule()

    def test_no_glitch_stationary(self):
        assert self.rule.evaluate({"GPS": _gps_df(
            lat=[37.7749] * 10, lng=[-122.4194] * 10
        )}) == []

    def test_large_jump_triggers(self):
        # Jump ~380km (LA→SF) in 200ms = clear glitch
        lats = [34.0522] * 5 + [37.7749] + [34.0522] * 4
        lngs = [-118.2437] * 5 + [-122.4194] + [-118.2437] * 4
        anomalies = self.rule.evaluate({"GPS": _gps_df(lat=lats, lng=lngs)})
        assert len(anomalies) > 0
