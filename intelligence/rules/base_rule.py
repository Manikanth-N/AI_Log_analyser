"""Base class for all deterministic aerospace anomaly detection rules."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

import polars as pl


Severity = Literal["INFO", "WARNING", "CRITICAL", "FATAL"]


@dataclass
class RuleAnomaly:
    rule_name: str
    category: str           # EKF, GPS, POWER, VIBE, MOTOR, MISSION, CONTROL
    severity: Severity
    timestamp_us: int
    end_timestamp_us: int | None  # None = instantaneous event
    description: str
    raw_values: dict[str, float] = field(default_factory=dict)
    correlation_hint: str = ""  # what to correlate with

    @property
    def is_sustained(self) -> bool:
        return self.end_timestamp_us is not None

    @property
    def duration_us(self) -> int | None:
        if self.end_timestamp_us is None:
            return None
        return self.end_timestamp_us - self.timestamp_us

    @property
    def duration_seconds(self) -> float | None:
        d = self.duration_us
        return d / 1_000_000.0 if d is not None else None


class BaseRule(ABC):
    """
    A deterministic anomaly detection rule.
    Rules are the GROUND TRUTH layer — they never hallucinate.
    LLMs interpret rule outputs; they do not replace them.
    """

    RULE_NAME: str
    CATEGORY: str

    @abstractmethod
    def evaluate(self, data: dict[str, pl.DataFrame]) -> list[RuleAnomaly]:
        """
        Evaluate the rule against parsed telemetry dataframes.
        data keys are canonical message type names (ATT, GPS, NKF4, etc.)
        Returns list of anomalies found (empty = nominal).
        """
        ...

    def _find_sustained_violations(
        self,
        df: pl.DataFrame,
        ts_col: str,
        mask_col: str,  # boolean column already computed
        min_duration_us: int,
        severity: Severity,
        description_template: str,
        raw_value_cols: list[str] | None = None,
    ) -> list[RuleAnomaly]:
        """
        Find runs of True values in mask_col that last >= min_duration_us.
        Helper shared across many rules.
        """
        if df.is_empty() or mask_col not in df.columns:
            return []

        anomalies = []
        ts = df[ts_col].to_list()
        mask = df[mask_col].to_list()

        i = 0
        while i < len(mask):
            if not mask[i]:
                i += 1
                continue

            # Start of a violation run
            start_i = i
            start_ts = ts[i]

            while i < len(mask) and mask[i]:
                i += 1

            end_i = i - 1
            end_ts = ts[end_i]

            duration = end_ts - start_ts
            if duration >= min_duration_us:
                raw = {}
                if raw_value_cols:
                    segment = df.slice(start_i, end_i - start_i + 1)
                    for col in raw_value_cols:
                        if col in segment.columns:
                            raw[f"{col}_max"] = float(segment[col].max())
                            raw[f"{col}_mean"] = float(segment[col].mean())

                anomalies.append(RuleAnomaly(
                    rule_name=self.RULE_NAME,
                    category=self.CATEGORY,
                    severity=severity,
                    timestamp_us=start_ts,
                    end_timestamp_us=end_ts,
                    description=description_template.format(**raw, duration_s=duration / 1e6),
                    raw_values=raw,
                ))

        return anomalies

    def _find_instantaneous_violations(
        self,
        df: pl.DataFrame,
        ts_col: str,
        mask_col: str,
        severity: Severity,
        description_template: str,
        raw_value_cols: list[str] | None = None,
    ) -> list[RuleAnomaly]:
        """Find individual rows where mask_col is True."""
        if df.is_empty() or mask_col not in df.columns:
            return []

        violations = df.filter(pl.col(mask_col))
        if violations.is_empty():
            return []

        anomalies = []
        for row in violations.iter_rows(named=True):
            raw = {c: row[c] for c in (raw_value_cols or []) if c in row}
            anomalies.append(RuleAnomaly(
                rule_name=self.RULE_NAME,
                category=self.CATEGORY,
                severity=severity,
                timestamp_us=int(row[ts_col]),
                end_timestamp_us=None,
                description=description_template.format(**raw),
                raw_values=raw,
            ))
        return anomalies
