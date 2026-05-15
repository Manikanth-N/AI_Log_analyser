"""Unit tests for statistical anomaly detectors."""

import numpy as np
import polars as pl
import pytest

from intelligence.signals.statistical import (
    rolling_zscore_anomalies,
    isolation_forest_anomalies,
    detect_changepoints,
)


def _df(ts: list[int], field: str, vals: list[float]) -> pl.DataFrame:
    return pl.DataFrame({"timestamp_us": ts, field: vals})


class TestRollingZscore:
    def test_flat_series_no_anomalies(self):
        ts = list(range(0, 10000, 10))
        df = _df(ts, "hdop", [5.0] * len(ts))
        result = rolling_zscore_anomalies(df, "hdop", window=20, threshold=3.0)
        assert result == []

    def test_spike_detected(self):
        ts = list(range(0, 10000, 10))
        vals = [5.0] * len(ts)
        vals[500] = 50.0
        df = _df(ts, "hdop", vals)
        result = rolling_zscore_anomalies(df, "hdop", window=20, threshold=3.0)
        assert len(result) > 0

    def test_series_shorter_than_window_returns_empty(self):
        ts = list(range(0, 50, 10))
        df = _df(ts, "hdop", [1.0, 2.0, 100.0, 1.0, 1.0])
        result = rolling_zscore_anomalies(df, "hdop", window=20, threshold=3.0)
        assert result == []

    def test_missing_field_returns_empty(self):
        ts = [0, 1, 2]
        df = _df(ts, "hdop", [1.0, 2.0, 3.0])
        result = rolling_zscore_anomalies(df, "voltage_v", window=5, threshold=3.0)
        assert result == []


class TestIsolationForest:
    def test_inliers_minimal_anomalies(self):
        np.random.seed(42)
        data = np.random.normal(0, 1, (200, 2))
        ts = list(range(0, 200_000, 1000))
        df = pl.DataFrame({"timestamp_us": ts, "x": data[:, 0].tolist(), "y": data[:, 1].tolist()})
        result = isolation_forest_anomalies(df, ["x", "y"], contamination=0.05)
        assert len(result) < 30

    def test_outlier_cluster_detected(self):
        inliers = np.zeros((190, 2))
        outliers = np.ones((10, 2)) * 100
        data = np.vstack([inliers, outliers])
        ts = list(range(0, 200_000, 1000))
        df = pl.DataFrame({"timestamp_us": ts, "x": data[:, 0].tolist(), "y": data[:, 1].tolist()})
        result = isolation_forest_anomalies(df, ["x", "y"], contamination=0.05)
        assert len(result) > 0

    def test_missing_feature_col_returns_empty(self):
        df = pl.DataFrame({"timestamp_us": [0, 1, 2], "x": [1.0, 2.0, 3.0]})
        result = isolation_forest_anomalies(df, ["x", "missing_col"], contamination=0.05)
        assert result == []


class TestChangepoints:
    def test_no_changepoint_flat(self):
        signal = np.array([2.0] * 100)
        ts = np.arange(0, 100_000, 1000)
        cps = detect_changepoints(signal, ts, min_size=10, penalty=5.0)
        assert len(cps) == 0

    def test_single_step_detected(self):
        signal = np.array([1.0] * 50 + [10.0] * 50)
        ts = np.arange(0, 100_000, 1000)
        cps = detect_changepoints(signal, ts, min_size=10, penalty=5.0)
        assert len(cps) >= 1
        # Changepoint should be near the step (index ~50 → timestamp ~50000)
        assert any(40_000 <= cp <= 60_000 for cp in cps)

    def test_too_short_returns_empty(self):
        signal = np.array([1.0, 2.0, 3.0])
        ts = np.array([0, 1000, 2000])
        assert detect_changepoints(signal, ts, min_size=10, penalty=5.0) == []
