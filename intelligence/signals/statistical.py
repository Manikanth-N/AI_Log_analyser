"""
Statistical anomaly detection: z-score rolling, IsolationForest, changepoints.
These complement deterministic rules with data-driven detection.
"""

from dataclasses import dataclass

import numpy as np
import polars as pl
from sklearn.ensemble import IsolationForest


@dataclass
class StatisticalAnomaly:
    timestamp_us: int
    field: str
    value: float
    z_score: float | None
    method: str
    severity: str


def rolling_zscore_anomalies(
    df: pl.DataFrame,
    field: str,
    window: int = 100,
    threshold: float = 3.0,
    severity_map: dict[float, str] | None = None,
) -> list[StatisticalAnomaly]:
    """
    Compute rolling z-score. Flag samples where |z| > threshold.
    Useful for detecting sensor drift and sudden value changes.
    """
    if field not in df.columns or df.is_empty():
        return []

    values = df[field].to_numpy().astype(float)
    ts = df["timestamp_us"].to_numpy()

    if len(values) < window:
        return []

    # Rolling mean and std using convolution
    kernel = np.ones(window) / window
    rolling_mean = np.convolve(values, kernel, mode="same")
    rolling_std = np.sqrt(np.convolve((values - rolling_mean) ** 2, kernel, mode="same"))
    rolling_std = np.maximum(rolling_std, 1e-10)

    z_scores = np.abs(values - rolling_mean) / rolling_std

    severity_map = severity_map or {threshold: "WARNING", threshold * 1.5: "CRITICAL"}

    anomalies = []
    flagged = z_scores > threshold

    # Deduplicate: only flag once per burst
    in_burst = False
    for i in range(len(values)):
        if flagged[i] and not in_burst:
            in_burst = True
            z = float(z_scores[i])
            sev = "WARNING"
            for thresh, s in sorted(severity_map.items()):
                if z >= thresh:
                    sev = s
            anomalies.append(StatisticalAnomaly(
                timestamp_us=int(ts[i]),
                field=field,
                value=float(values[i]),
                z_score=z,
                method="rolling_zscore",
                severity=sev,
            ))
        elif not flagged[i]:
            in_burst = False

    return anomalies


def isolation_forest_anomalies(
    df: pl.DataFrame,
    feature_cols: list[str],
    contamination: float = 0.05,
    n_estimators: int = 100,
) -> list[StatisticalAnomaly]:
    """
    IsolationForest for multivariate anomaly detection.
    Detects flight segments that are anomalous across multiple channels.
    Best for: detecting unusual flight regimes, sensor combination anomalies.
    """
    available = [c for c in feature_cols if c in df.columns]
    if len(available) < 2 or df.is_empty():
        return []

    X = df.select(available).to_numpy()

    # Handle NaN
    valid_mask = ~np.any(np.isnan(X), axis=1)
    if valid_mask.sum() < 20:
        return []

    X_clean = X[valid_mask]
    ts = df["timestamp_us"].to_numpy()[valid_mask]

    clf = IsolationForest(
        n_estimators=n_estimators,
        contamination=contamination,
        random_state=42,
        n_jobs=-1,
    )
    preds = clf.fit_predict(X_clean)
    scores = clf.score_samples(X_clean)

    anomalies = []
    outlier_mask = preds == -1

    # Group consecutive outliers
    indices = np.where(outlier_mask)[0]
    if len(indices) == 0:
        return []

    groups = np.split(indices, np.where(np.diff(indices) > 5)[0] + 1)
    for grp in groups:
        if len(grp) == 0:
            continue
        worst_score = float(scores[grp].min())
        centroid_ts = int(ts[grp[len(grp) // 2]])
        feature_vals = {f: float(X_clean[grp[0], i]) for i, f in enumerate(available)}

        anomalies.append(StatisticalAnomaly(
            timestamp_us=centroid_ts,
            field=",".join(available),
            value=worst_score,
            z_score=None,
            method="isolation_forest",
            severity="WARNING" if worst_score > -0.15 else "CRITICAL",
        ))

    return anomalies


def detect_changepoints(
    values: np.ndarray,
    timestamps_us: np.ndarray,
    min_size: int = 20,
    penalty: float = 10.0,
) -> list[int]:
    """
    PELT algorithm for offline changepoint detection.
    Returns list of timestamp_us values where distribution changes.
    Used for: detecting when EKF enters different operating mode,
    when vibration character changes (prop damage), when GPS quality shifts.
    """
    try:
        import ruptures as rpt
    except ImportError:
        return []

    if len(values) < min_size * 2:
        return []

    # Normalize
    std = np.std(values)
    if std < 1e-10:
        return []
    normalized = (values - np.mean(values)) / std

    algo = rpt.Pelt(model="rbf", min_size=min_size).fit(normalized.reshape(-1, 1))
    breakpoints = algo.predict(pen=penalty)

    # Convert indices to timestamps, exclude the final "end" breakpoint
    return [int(timestamps_us[bp - 1]) for bp in breakpoints if bp < len(timestamps_us)]


def compute_sensor_disagreement(
    series_a: np.ndarray,
    series_b: np.ndarray,
    timestamps_a: np.ndarray,
    timestamps_b: np.ndarray,
    label_a: str = "sensor_a",
    label_b: str = "sensor_b",
) -> dict:
    """
    Quantify disagreement between two sensors measuring the same quantity.
    Interpolates both to common time base, computes RMS difference.
    """
    t_common = np.union1d(timestamps_a, timestamps_b)
    t_min = max(timestamps_a.min(), timestamps_b.min())
    t_max = min(timestamps_a.max(), timestamps_b.max())
    t_common = t_common[(t_common >= t_min) & (t_common <= t_max)]

    if len(t_common) < 10:
        return {"rmse": None, "max_diff": None, "note": "Insufficient overlap"}

    a_interp = np.interp(t_common, timestamps_a, series_a)
    b_interp = np.interp(t_common, timestamps_b, series_b)

    diff = a_interp - b_interp
    rmse = float(np.sqrt(np.mean(diff ** 2)))
    max_diff = float(np.max(np.abs(diff)))
    mean_diff = float(np.mean(diff))  # bias

    return {
        "rmse": rmse,
        "max_diff": max_diff,
        "mean_bias": mean_diff,
        "label_a": label_a,
        "label_b": label_b,
        "n_samples": len(t_common),
    }


def cross_correlate_lag(
    signal_a: np.ndarray,
    signal_b: np.ndarray,
    sample_rate_hz: float,
    max_lag_s: float = 1.0,
) -> dict:
    """
    Find the time lag between two signals via cross-correlation.
    Useful for detecting sensor measurement delays (GPS lag, etc.)
    """
    max_lag_samples = int(max_lag_s * sample_rate_hz)

    # Normalize
    a = (signal_a - np.mean(signal_a)) / (np.std(signal_a) + 1e-10)
    b = (signal_b - np.mean(signal_b)) / (np.std(signal_b) + 1e-10)

    n = min(len(a), len(b))
    a, b = a[:n], b[:n]

    correlation = np.correlate(a, b, mode="full")
    lags = np.arange(-(n - 1), n)

    # Restrict to max_lag
    mask = np.abs(lags) <= max_lag_samples
    lag_idx = np.argmax(correlation[mask])
    best_lag_samples = lags[mask][lag_idx]
    best_lag_s = best_lag_samples / sample_rate_hz
    peak_correlation = float(correlation[mask][lag_idx] / n)

    return {
        "lag_samples": int(best_lag_samples),
        "lag_seconds": float(best_lag_s),
        "peak_correlation": peak_correlation,
        "is_significant": peak_correlation > 0.5,
    }
