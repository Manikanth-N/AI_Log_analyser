"""
Extracts a unified chronological event timeline from all message types.
Events are structured facts: mode changes, errors, arm/disarm, GPS anomalies, etc.
"""

from dataclasses import dataclass, asdict
from typing import Any

import polars as pl
import structlog

from parsers.schema import ARDUPILOT_FLIGHT_MODES_COPTER, ARDUPILOT_ERR_SUBSYS, TIMESTAMP_COL
from storage.parquet_store import ParquetStore

log = structlog.get_logger(__name__)


@dataclass
class FlightEvent:
    timestamp_us: int
    event_type: str      # ARM, DISARM, MODE_CHANGE, ERROR, GPS_GLITCH, FAILSAFE, CRASH, etc.
    subsystem: str       # GPS, EKF, POWER, CONTROL, MISSION, etc.
    severity: str        # INFO, WARNING, CRITICAL
    description: str
    data: dict[str, Any]


class EventExtractor:
    def __init__(self, flight_id: str, store: ParquetStore):
        self.flight_id = flight_id
        self.store = store

    def extract(self) -> dict:
        events: list[FlightEvent] = []

        events += self._extract_mode_changes()
        events += self._extract_errors()
        events += self._extract_arm_events()
        events += self._extract_gps_events()
        events += self._extract_crash_indicator()
        events += self._extract_parameter_changes()

        # Sort chronologically
        events.sort(key=lambda e: e.timestamp_us)

        # Find event horizon: timestamp of last nominal state before first CRITICAL event
        event_horizon_us = self._find_event_horizon(events)

        return {
            "events": [asdict(e) for e in events],
            "total_events": len(events),
            "event_horizon_us": event_horizon_us,
            "critical_events": [asdict(e) for e in events if e.severity == "CRITICAL"],
            "warnings": [asdict(e) for e in events if e.severity == "WARNING"],
        }

    def _extract_mode_changes(self) -> list[FlightEvent]:
        df = self.store.load(self.flight_id, "MODE")
        if df.is_empty():
            return []

        events = []
        prev_mode = None
        for row in df.iter_rows(named=True):
            mode_name = row.get("mode_name", f"MODE_{row.get('mode_num', '?')}")
            mode_num = row.get("mode_num", -1)
            reason = row.get("reason", 0)

            if prev_mode is not None and mode_name != prev_mode:
                events.append(FlightEvent(
                    timestamp_us=row[TIMESTAMP_COL],
                    event_type="MODE_CHANGE",
                    subsystem="FLIGHT_CONTROLLER",
                    severity="INFO" if reason in (1, 2) else "WARNING",
                    description=f"Mode changed: {prev_mode} → {mode_name} (reason={reason})",
                    data={"from": prev_mode, "to": mode_name, "mode_num": mode_num, "reason": reason},
                ))

            prev_mode = mode_name

        return events

    def _extract_errors(self) -> list[FlightEvent]:
        df = self.store.load(self.flight_id, "ERR")
        if df.is_empty():
            return []

        events = []
        for row in df.iter_rows(named=True):
            subsys = row.get("subsys", 0)
            ecode = row.get("ecode", 0)
            subsys_name = row.get("subsys_name") or ARDUPILOT_ERR_SUBSYS.get(subsys, f"SUBSYS_{subsys}")

            # ECode 0 = clear/recovered, non-zero = active error
            if ecode == 0:
                desc = f"ERROR CLEARED: {subsys_name}"
                severity = "INFO"
            else:
                desc = f"ERROR: {subsys_name} code={ecode}"
                severity = "CRITICAL" if subsys in (6, 7, 8, 16, 17, 24) else "WARNING"

            events.append(FlightEvent(
                timestamp_us=row[TIMESTAMP_COL],
                event_type="ERROR",
                subsystem=subsys_name,
                severity=severity,
                description=desc,
                data={"subsys": subsys, "subsys_name": subsys_name, "ecode": ecode},
            ))

        return events

    def _extract_arm_events(self) -> list[FlightEvent]:
        """Detect arm/disarm from MODE messages (INITIALISING → first real mode = arm)."""
        df = self.store.load(self.flight_id, "MODE")
        if df.is_empty():
            return []

        events = []
        modes = df.iter_rows(named=True)
        prev_armed = False

        for row in modes:
            mode_num = row.get("mode_num", -1)
            # In ArduPilot, mode 0 (STABILIZE) or any non-init mode = armed
            is_armed = mode_num >= 0 and row.get("mode_name", "") not in ("INITIALISING", "")

            if is_armed and not prev_armed:
                events.append(FlightEvent(
                    timestamp_us=row[TIMESTAMP_COL],
                    event_type="ARM",
                    subsystem="ARMING",
                    severity="INFO",
                    description=f"Vehicle ARMED in mode {row.get('mode_name', mode_num)}",
                    data={"mode": row.get("mode_name", mode_num)},
                ))
            elif not is_armed and prev_armed:
                events.append(FlightEvent(
                    timestamp_us=row[TIMESTAMP_COL],
                    event_type="DISARM",
                    subsystem="ARMING",
                    severity="INFO",
                    description="Vehicle DISARMED",
                    data={},
                ))

            prev_armed = is_armed

        return events

    def _extract_gps_events(self) -> list[FlightEvent]:
        df = self.store.load(self.flight_id, "GPS")
        if df.is_empty():
            return []

        events = []
        prev_fix = None
        prev_sats = None
        prev_hdop = None

        for row in df.iter_rows(named=True):
            fix = row.get("fix_type")
            sats = row.get("num_sats")
            hdop = row.get("hdop")

            # Fix type degradation
            if prev_fix is not None and fix is not None and fix < prev_fix:
                events.append(FlightEvent(
                    timestamp_us=row[TIMESTAMP_COL],
                    event_type="GPS_FIX_DEGRADED",
                    subsystem="GPS",
                    severity="CRITICAL" if fix < 2 else "WARNING",
                    description=f"GPS fix degraded: {prev_fix}D → {fix}D",
                    data={"prev_fix": prev_fix, "new_fix": fix},
                ))

            # Satellite count drop
            if prev_sats is not None and sats is not None and (prev_sats - sats) >= 4:
                events.append(FlightEvent(
                    timestamp_us=row[TIMESTAMP_COL],
                    event_type="GPS_SAT_DROP",
                    subsystem="GPS",
                    severity="CRITICAL",
                    description=f"GPS satellite count dropped: {prev_sats} → {sats}",
                    data={"prev_sats": prev_sats, "new_sats": sats},
                ))

            # HDOP spike
            if prev_hdop is not None and hdop is not None and hdop > 2.0 and prev_hdop <= 2.0:
                events.append(FlightEvent(
                    timestamp_us=row[TIMESTAMP_COL],
                    event_type="GPS_HDOP_SPIKE",
                    subsystem="GPS",
                    severity="WARNING",
                    description=f"GPS HDOP crossed 2.0 threshold: {prev_hdop:.2f} → {hdop:.2f}",
                    data={"prev_hdop": prev_hdop, "new_hdop": hdop},
                ))

            prev_fix = fix
            prev_sats = sats
            prev_hdop = hdop

        return events

    def _extract_crash_indicator(self) -> list[FlightEvent]:
        """Detect crash from attitude extremes post-flight."""
        att = self.store.load(self.flight_id, "ATT")
        if att.is_empty():
            return []

        if "roll_deg" not in att.columns or "pitch_deg" not in att.columns:
            return []

        # Crash: attitude > 60 degrees and no recovery
        extreme = att.filter(
            (pl.col("roll_deg").abs() > 60) | (pl.col("pitch_deg").abs() > 60)
        )

        if extreme.is_empty():
            return []

        first_extreme_ts = int(extreme[TIMESTAMP_COL][0])
        return [FlightEvent(
            timestamp_us=first_extreme_ts,
            event_type="CRASH_DETECTED",
            subsystem="ATTITUDE",
            severity="FATAL",
            description=f"Crash indicator: attitude exceeded 60° at T+{first_extreme_ts/1e6:.1f}s",
            data={
                "roll_deg": float(extreme["roll_deg"][0]),
                "pitch_deg": float(extreme["pitch_deg"][0]),
            },
        )]

    def _extract_parameter_changes(self) -> list[FlightEvent]:
        """Log all in-flight parameter changes (PARM messages after initial dump)."""
        df = self.store.load(self.flight_id, "PARM")
        if df.is_empty():
            return []

        if "name" not in df.columns:
            return []

        # The initial parameter dump happens at the start — skip first 10 seconds
        t_start = int(df[TIMESTAMP_COL].min() or 0)
        later = df.filter(pl.col(TIMESTAMP_COL) > t_start + 10_000_000)

        events = []
        for row in later.iter_rows(named=True):
            events.append(FlightEvent(
                timestamp_us=row[TIMESTAMP_COL],
                event_type="PARAM_CHANGE",
                subsystem="PARAMETERS",
                severity="INFO",
                description=f"Parameter changed in-flight: {row['name']} = {row.get('value', '?')}",
                data={"name": row["name"], "value": row.get("value")},
            ))

        return events

    def _find_event_horizon(self, events: list[FlightEvent]) -> int | None:
        """
        Find the last normal state before the failure chain started.
        = timestamp of the last INFO event before the first CRITICAL event.
        """
        critical_events = [e for e in events if e.severity in ("CRITICAL", "FATAL")]
        if not critical_events:
            return None

        first_critical_ts = critical_events[0].timestamp_us

        # Find last INFO event before first critical
        info_before = [
            e for e in events
            if e.severity == "INFO" and e.timestamp_us < first_critical_ts
        ]

        return info_before[-1].timestamp_us if info_before else None
