"""Unit tests for FFT vibration analyzer."""

import numpy as np
import polars as pl
import pytest

from intelligence.signals.fft_analyzer import analyze_vibration


def _imu_df(n: int = 2048, sample_rate: float = 400.0,
            dominant_hz: float = 80.0, amplitude: float = 3.0) -> pl.DataFrame:
    """Generate synthetic IMU data with a dominant frequency component."""
    t = np.linspace(0, n / sample_rate, n)
    ts_us = (t * 1e6).astype(int).tolist()
    # Use actual column names from the parser schema: acc_x_m_s2
    accel_x = (amplitude * np.sin(2 * np.pi * dominant_hz * t)).tolist()
    accel_y = (amplitude * 0.5 * np.sin(2 * np.pi * dominant_hz * t + 0.5)).tolist()
    accel_z = (9.81 + amplitude * 0.3 * np.sin(2 * np.pi * dominant_hz * t)).tolist()
    return pl.DataFrame({
        "timestamp_us": ts_us,
        "acc_x_m_s2": accel_x,
        "acc_y_m_s2": accel_y,
        "acc_z_m_s2": accel_z,
    })


class TestAnalyzeVibration:
    def test_returns_result_structure(self):
        df = _imu_df()
        result = analyze_vibration(df)
        assert hasattr(result, "x")
        assert hasattr(result, "y")
        assert hasattr(result, "z")
        assert hasattr(result, "overall_severity")

    def test_low_vibration_good_severity(self):
        df = _imu_df(amplitude=0.5)
        result = analyze_vibration(df)
        assert result.overall_severity in ("GOOD", "ACCEPTABLE")

    def test_high_vibration_warning_severity(self):
        df = _imu_df(amplitude=25.0)
        result = analyze_vibration(df)
        assert result.overall_severity in ("WARNING", "CRITICAL")

    def test_empty_dataframe_returns_default(self):
        df = pl.DataFrame({
            "timestamp_us": [], "acc_x_m_s2": [], "acc_y_m_s2": [], "acc_z_m_s2": []
        })
        result = analyze_vibration(df)
        assert result is not None
        assert result.overall_severity == "GOOD"  # _empty_result default

    def test_time_window_filtering(self):
        df = _imu_df()
        ts_mid = df["timestamp_us"][len(df) // 2]
        result = analyze_vibration(df, start_us=int(ts_mid))
        assert result is not None
