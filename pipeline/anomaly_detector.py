"""
Fast anomaly detection pass: runs all deterministic rules + basic statistical checks.
Produces initial anomaly list before agents do deep reasoning.
"""

import structlog

from intelligence.rules import ALL_RULES, RuleAnomaly
from intelligence.signals.statistical import rolling_zscore_anomalies, StatisticalAnomaly
from storage.parquet_store import ParquetStore

log = structlog.get_logger(__name__)

# Message types needed by each rule category
_CATEGORY_MESSAGE_TYPES = {
    "EKF": ["NKF1", "NKF3", "NKF4", "NKF5", "XKF1", "XKF3", "XKF4", "ERR"],
    "GPS": ["GPS", "GPS2"],
    "POWER": ["BAT", "CURR"],
    "VIBRATION": ["VIBE", "IMU", "IMU2"],
    "MOTOR": ["RCOU", "ESC", "ATT"],
    "FAILSAFE": ["ERR", "MODE", "GPS"],
}

ALL_NEEDED_TYPES = sorted(set(t for types in _CATEGORY_MESSAGE_TYPES.values() for t in types))


class FastAnomalyDetector:
    def __init__(self, flight_id: str, store: ParquetStore):
        self.flight_id = flight_id
        self.store = store

    def run_all_rules(self) -> list[RuleAnomaly]:
        """Load all needed Parquet files and run every deterministic rule."""
        data = self.store.load_many(self.flight_id, ALL_NEEDED_TYPES)

        all_anomalies: list[RuleAnomaly] = []

        for rule in ALL_RULES:
            try:
                anomalies = rule.evaluate(data)
                all_anomalies.extend(anomalies)
                if anomalies:
                    log.debug(
                        "rule_fired",
                        rule=rule.RULE_NAME,
                        count=len(anomalies),
                        severities=[a.severity for a in anomalies],
                    )
            except Exception as e:
                log.warning("rule_error", rule=rule.RULE_NAME, error=str(e))

        # Sort by timestamp, then severity
        sev_order = {"FATAL": 0, "CRITICAL": 1, "WARNING": 2, "INFO": 3}
        all_anomalies.sort(key=lambda a: (a.timestamp_us, sev_order.get(a.severity, 4)))

        log.info(
            "fast_anomaly_pass_complete",
            flight_id=self.flight_id,
            total=len(all_anomalies),
            critical=sum(1 for a in all_anomalies if a.severity == "CRITICAL"),
            warnings=sum(1 for a in all_anomalies if a.severity == "WARNING"),
        )

        return all_anomalies

    def run_statistical_checks(self) -> list[StatisticalAnomaly]:
        """Rolling z-score checks on key numeric channels."""
        stat_anomalies: list[StatisticalAnomaly] = []

        checks = [
            ("GPS", "hdop", 3.0),
            ("BAT", "voltage_v", 4.0),
            ("BAT", "current_a", 3.5),
            ("NKF4", "var_ratio_vel", 3.0),
            ("ATT", "roll_deg", 4.0),
            ("ATT", "pitch_deg", 4.0),
        ]

        for msg_type, field, threshold in checks:
            df = self.store.load(self.flight_id, msg_type, columns=["timestamp_us", field])
            if df.is_empty():
                continue
            anomalies = rolling_zscore_anomalies(
                df.rename({"timestamp_us": "timestamp_us"}),
                field=field,
                window=50,
                threshold=threshold,
            )
            stat_anomalies.extend(anomalies)

        return stat_anomalies

    def build_anomaly_summary(self, anomalies: list[RuleAnomaly]) -> dict:
        """Build summary statistics for anomaly list."""
        by_category: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        critical_timestamps: list[int] = []

        for a in anomalies:
            by_category[a.category] = by_category.get(a.category, 0) + 1
            by_severity[a.severity] = by_severity.get(a.severity, 0) + 1
            if a.severity in ("CRITICAL", "FATAL"):
                critical_timestamps.append(a.timestamp_us)

        first_critical_us = min(critical_timestamps) if critical_timestamps else None

        return {
            "total": len(anomalies),
            "by_category": by_category,
            "by_severity": by_severity,
            "first_critical_us": first_critical_us,
            "has_fatal": by_severity.get("FATAL", 0) > 0,
        }
