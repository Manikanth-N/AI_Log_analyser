"""
Vibration FFT analysis using Welch PSD method.
Identifies dominant frequencies, motor harmonics, structural resonances.
"""

from dataclasses import dataclass, field

import numpy as np
import polars as pl
from scipy import signal


@dataclass
class FrequencyPeak:
    frequency_hz: float
    power_db: float
    is_motor_harmonic: bool = False
    harmonic_order: int | None = None
    motor_fundamental_hz: float | None = None
    label: str = ""


@dataclass
class VibrationSpectrum:
    axis: str                           # X, Y, Z
    frequencies_hz: list[float]
    psd_db: list[float]
    rms_m_s2: float
    peaks: list[FrequencyPeak]
    dominant_freq_hz: float | None
    severity: str                       # GOOD / ACCEPTABLE / WARNING / CRITICAL
    clip_count: int = 0
    sample_rate_hz: float = 0.0
    window_start_us: int = 0
    window_end_us: int = 0


@dataclass
class VibrationAnalysisResult:
    x: VibrationSpectrum
    y: VibrationSpectrum
    z: VibrationSpectrum
    identified_motor_fundamental_hz: float | None
    overall_severity: str
    notch_filter_recommended: bool
    notch_frequencies_hz: list[float] = field(default_factory=list)
    analysis_note: str = ""

    def to_dict(self) -> dict:
        return {
            "x_rms": self.x.rms_m_s2,
            "y_rms": self.y.rms_m_s2,
            "z_rms": self.z.rms_m_s2,
            "motor_fundamental_hz": self.identified_motor_fundamental_hz,
            "overall_severity": self.overall_severity,
            "notch_filter_recommended": self.notch_filter_recommended,
            "notch_frequencies_hz": self.notch_frequencies_hz,
            "analysis_note": self.analysis_note,
            "peaks_z": [
                {"hz": p.frequency_hz, "db": p.power_db, "label": p.label}
                for p in self.z.peaks
            ],
        }


def analyze_vibration(
    imu_df: pl.DataFrame,
    vibe_df: pl.DataFrame | None = None,
    start_us: int | None = None,
    end_us: int | None = None,
    expected_motor_hz: float | None = None,
) -> VibrationAnalysisResult:
    """
    Full vibration analysis: PSD, peak finding, motor harmonic identification.

    Args:
        imu_df: IMU dataframe with acc_x_m_s2, acc_y_m_s2, acc_z_m_s2
        vibe_df: Optional ArduPilot VIBE dataframe for clip counts
        start_us / end_us: Time window (None = entire log)
        expected_motor_hz: If known, used for harmonic matching
    """
    if start_us is not None or end_us is not None:
        filt = pl.lit(True)
        if start_us is not None:
            filt = filt & (pl.col("timestamp_us") >= start_us)
        if end_us is not None:
            filt = filt & (pl.col("timestamp_us") <= end_us)
        imu_df = imu_df.filter(filt)

    if imu_df.is_empty():
        return _empty_result()

    ts_us = imu_df["timestamp_us"].to_numpy()
    dt_us = np.median(np.diff(ts_us))
    if dt_us <= 0:
        return _empty_result()

    sample_rate_hz = 1_000_000.0 / dt_us
    # Clamp to realistic IMU rates
    sample_rate_hz = float(np.clip(sample_rate_hz, 50, 2000))

    clips = _get_clip_counts(vibe_df, start_us, end_us)
    win_start = int(ts_us[0])
    win_end = int(ts_us[-1])

    axes: dict[str, VibrationSpectrum] = {}
    motor_candidates: list[float] = []

    for axis, col in [("X", "acc_x_m_s2"), ("Y", "acc_y_m_s2"), ("Z", "acc_z_m_s2")]:
        if col not in imu_df.columns:
            axes[axis] = _empty_spectrum(axis)
            continue

        data = imu_df[col].to_numpy()
        # Remove DC component (gravity on Z)
        data = data - np.mean(data)

        # Welch PSD with Hann window
        nperseg = min(512, len(data) // 4)
        if nperseg < 16:
            axes[axis] = _empty_spectrum(axis)
            continue

        freqs, psd = signal.welch(
            data,
            fs=sample_rate_hz,
            window="hann",
            nperseg=nperseg,
            scaling="density",
        )

        psd_db = 10 * np.log10(np.maximum(psd, 1e-20))
        rms = float(np.sqrt(np.mean(data ** 2)))

        # Find peaks with prominence filtering
        min_freq = 5.0    # ignore below 5 Hz (rigid body modes)
        max_freq = min(sample_rate_hz / 2, 500.0)
        freq_mask = (freqs >= min_freq) & (freqs <= max_freq)

        peaks_idx, props = signal.find_peaks(
            psd_db[freq_mask],
            prominence=6.0,   # at least 6 dB above surroundings
            distance=int(5.0 / (freqs[1] - freqs[0])),  # min 5 Hz separation
        )

        # Shift indices back to full frequency array
        masked_freqs = freqs[freq_mask]
        peaks_hz = masked_freqs[peaks_idx]
        peaks_db = psd_db[freq_mask][peaks_idx]

        # Sort by power
        sort_idx = np.argsort(peaks_db)[::-1]
        peaks_hz = peaks_hz[sort_idx]
        peaks_db = peaks_db[sort_idx]

        # Build peak objects
        freq_peaks: list[FrequencyPeak] = []
        for hz, db in zip(peaks_hz[:10], peaks_db[:10]):
            fp = FrequencyPeak(frequency_hz=float(hz), power_db=float(db))
            freq_peaks.append(fp)

        # Identify motor harmonics
        motor_hz = expected_motor_hz or _estimate_motor_fundamental(peaks_hz)
        if motor_hz:
            motor_candidates.append(motor_hz)
            for fp in freq_peaks:
                for n in range(1, 8):
                    if abs(fp.frequency_hz - motor_hz * n) < motor_hz * 0.05:
                        fp.is_motor_harmonic = True
                        fp.harmonic_order = n
                        fp.motor_fundamental_hz = motor_hz
                        fp.label = f"{n}P motor harmonic ({motor_hz:.1f} Hz fund.)"
                        break
                if not fp.is_motor_harmonic:
                    fp.label = f"Unidentified peak @ {fp.frequency_hz:.1f} Hz"

        dominant = float(peaks_hz[0]) if len(peaks_hz) > 0 else None

        axes[axis] = VibrationSpectrum(
            axis=axis,
            frequencies_hz=freqs.tolist(),
            psd_db=psd_db.tolist(),
            rms_m_s2=rms,
            peaks=freq_peaks,
            dominant_freq_hz=dominant,
            severity=_classify_severity(rms),
            clip_count=clips.get(axis.lower(), 0),
            sample_rate_hz=sample_rate_hz,
            window_start_us=win_start,
            window_end_us=win_end,
        )

    confirmed_motor_hz = float(np.median(motor_candidates)) if motor_candidates else None

    # Identify notch filter candidates: non-harmonic peaks > WARNING level
    notch_freqs = []
    for fp in axes.get("Z", _empty_spectrum("Z")).peaks:
        if not fp.is_motor_harmonic and fp.power_db > -40:
            notch_freqs.append(fp.frequency_hz)

    # If motor fundamental found, recommend notch there too
    if confirmed_motor_hz:
        notch_freqs.insert(0, confirmed_motor_hz)

    severities = [axes[ax].severity for ax in ("X", "Y", "Z") if ax in axes]
    sev_order = {"GOOD": 0, "ACCEPTABLE": 1, "WARNING": 2, "CRITICAL": 3}
    overall = max(severities, key=lambda s: sev_order.get(s, 0), default="GOOD")

    note = _build_analysis_note(axes, confirmed_motor_hz)

    return VibrationAnalysisResult(
        x=axes.get("X", _empty_spectrum("X")),
        y=axes.get("Y", _empty_spectrum("Y")),
        z=axes.get("Z", _empty_spectrum("Z")),
        identified_motor_fundamental_hz=confirmed_motor_hz,
        overall_severity=overall,
        notch_filter_recommended=overall in ("WARNING", "CRITICAL") or bool(notch_freqs),
        notch_frequencies_hz=sorted(set(notch_freqs))[:5],
        analysis_note=note,
    )


def _estimate_motor_fundamental(peaks_hz: np.ndarray) -> float | None:
    """
    Heuristic: motor fundamental is usually in 20–200 Hz range.
    Look for the lowest significant peak in that range.
    """
    if len(peaks_hz) == 0:
        return None
    motor_range = peaks_hz[(peaks_hz >= 20) & (peaks_hz <= 200)]
    if len(motor_range) == 0:
        return None
    return float(motor_range[0])


def _classify_severity(rms: float) -> str:
    if rms < 5.0:
        return "GOOD"
    elif rms < 15.0:
        return "ACCEPTABLE"
    elif rms < 30.0:
        return "WARNING"
    return "CRITICAL"


def _get_clip_counts(
    vibe_df: pl.DataFrame | None,
    start_us: int | None,
    end_us: int | None,
) -> dict[str, int]:
    if vibe_df is None or vibe_df.is_empty():
        return {}
    df = vibe_df
    if start_us is not None:
        df = df.filter(pl.col("timestamp_us") >= start_us)
    if end_us is not None:
        df = df.filter(pl.col("timestamp_us") <= end_us)
    result = {}
    for axis, col in [("x", "clip0"), ("y", "clip1"), ("z", "clip2")]:
        if col in df.columns:
            result[axis] = int(df[col].sum())
    return result


def _build_analysis_note(axes: dict, motor_hz: float | None) -> str:
    notes = []
    for ax, spec in axes.items():
        if spec.severity in ("WARNING", "CRITICAL"):
            notes.append(f"Axis {ax}: {spec.rms_m_s2:.1f} m/s² RMS ({spec.severity})")
    if motor_hz:
        notes.append(f"Motor fundamental identified: {motor_hz:.1f} Hz")
    unid = [p for s in axes.values() for p in s.peaks if not p.is_motor_harmonic]
    if unid:
        notes.append(f"{len(unid)} unidentified frequency peaks (possible structural resonance or bent prop)")
    return ". ".join(notes) or "Vibration within normal parameters."


def _empty_spectrum(axis: str) -> VibrationSpectrum:
    return VibrationSpectrum(
        axis=axis, frequencies_hz=[], psd_db=[], rms_m_s2=0.0,
        peaks=[], dominant_freq_hz=None, severity="GOOD",
    )


def _empty_result() -> VibrationAnalysisResult:
    return VibrationAnalysisResult(
        x=_empty_spectrum("X"), y=_empty_spectrum("Y"), z=_empty_spectrum("Z"),
        identified_motor_fundamental_hz=None,
        overall_severity="GOOD",
        notch_filter_recommended=False,
        analysis_note="Insufficient IMU data for vibration analysis.",
    )
