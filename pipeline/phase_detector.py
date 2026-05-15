"""
Flight phase segmentation from MODE messages and altitude/attitude data.
Produces a list of discrete flight phases with start/end timestamps.
"""

import polars as pl
import structlog

from parsers.schema import TIMESTAMP_COL
from storage.parquet_store import ParquetStore

log = structlog.get_logger(__name__)

# Phases in order — used for validation
PHASE_ORDER = [
    "PRE_ARM",
    "ARMING",
    "TAKEOFF",
    "CLIMB",
    "CRUISE",
    "HOVER",
    "MANEUVER",
    "AUTO_MISSION",
    "RTL",
    "DESCENT",
    "LANDING",
    "DISARM",
    "ANOMALY",
    "CRASH",
]

# ArduPilot mode → phase category
MODE_TO_PHASE = {
    "STABILIZE": "HOVER",
    "ALT_HOLD": "HOVER",
    "LOITER": "HOVER",
    "POSHOLD": "HOVER",
    "GUIDED": "HOVER",
    "ACRO": "MANEUVER",
    "SPORT": "MANEUVER",
    "FLIP": "MANEUVER",
    "AUTO": "AUTO_MISSION",
    "RTL": "RTL",
    "SMART_RTL": "RTL",
    "LAND": "LANDING",
    "BRAKE": "HOVER",
    "DRIFT": "MANEUVER",
    "CIRCLE": "MANEUVER",
    "FOLLOW": "AUTO_MISSION",
    "ZIGZAG": "AUTO_MISSION",
    "AUTOTUNE": "HOVER",
}


class PhaseDetector:
    def __init__(self, flight_id: str, store: ParquetStore):
        self.flight_id = flight_id
        self.store = store

    def detect(self, timeline: dict) -> list[dict]:
        """
        Build flight phase list from MODE messages + altitude/attitude context.
        Returns ordered list of phase dicts.
        """
        mode_df = self.store.load(self.flight_id, "MODE")
        att_df = self.store.load(self.flight_id, "ATT")
        gps_df = self.store.load(self.flight_id, "GPS")

        if mode_df.is_empty():
            return self._build_single_phase(att_df, gps_df)

        events = timeline.get("events", [])
        arm_ts = next((e["timestamp_us"] for e in events if e["event_type"] == "ARM"), None)
        crash_ts = next((e["timestamp_us"] for e in events if e["event_type"] == "CRASH_DETECTED"), None)

        phases = []

        # PRE_ARM phase
        t_start = int(mode_df[TIMESTAMP_COL][0])
        if arm_ts and arm_ts > t_start:
            phases.append({
                "name": "PRE_ARM",
                "start_us": t_start,
                "end_us": arm_ts,
                "mode_name": "PREFLIGHT",
                "notes": "Pre-flight checks, GPS acquisition",
            })

        # Phase from each MODE segment
        mode_rows = mode_df.iter_rows(named=True)
        mode_list = list(mode_df.iter_rows(named=True))

        for i, row in enumerate(mode_list):
            ts = row[TIMESTAMP_COL]
            mode_name = row.get("mode_name", f"MODE_{row.get('mode_num', '?')}")

            # Skip pre-arm modes
            if mode_name in ("INITIALISING", ""):
                continue

            # Determine phase end
            if i + 1 < len(mode_list):
                end_ts = mode_list[i + 1][TIMESTAMP_COL]
            elif crash_ts:
                end_ts = crash_ts
            else:
                end_ts = int(mode_df[TIMESTAMP_COL][-1])

            phase_name = MODE_TO_PHASE.get(mode_name, "HOVER")

            # Classify TAKEOFF: first phase after arm where altitude is rising
            if arm_ts and ts >= arm_ts and phase_name == "HOVER":
                alt_in_phase = self._get_alt_range(gps_df, ts, end_ts)
                if alt_in_phase and alt_in_phase["delta"] > 2.0:
                    phase_name = "TAKEOFF"
                elif alt_in_phase and alt_in_phase["mean"] < 0.5:
                    phase_name = "LANDING"

            notes = self._build_phase_notes(phase_name, ts, end_ts, att_df, gps_df)

            phases.append({
                "name": phase_name,
                "start_us": ts,
                "end_us": end_ts,
                "mode_name": mode_name,
                "notes": notes,
            })

        # CRASH phase
        if crash_ts:
            last_end = phases[-1]["end_us"] if phases else crash_ts
            phases.append({
                "name": "CRASH",
                "start_us": crash_ts,
                "end_us": crash_ts + 5_000_000,
                "mode_name": "CRASH",
                "notes": "Crash detected from attitude extremes",
            })

        return phases

    def _get_alt_range(
        self,
        gps_df: pl.DataFrame,
        start_us: int,
        end_us: int,
    ) -> dict | None:
        if gps_df.is_empty() or "alt_rel_m" not in gps_df.columns:
            return None
        seg = gps_df.filter(
            (pl.col(TIMESTAMP_COL) >= start_us) & (pl.col(TIMESTAMP_COL) <= end_us)
        )
        if seg.is_empty():
            return None
        alts = seg["alt_rel_m"].to_numpy()
        return {
            "min": float(alts.min()),
            "max": float(alts.max()),
            "mean": float(alts.mean()),
            "delta": float(alts.max() - alts.min()),
        }

    def _build_phase_notes(
        self,
        phase_name: str,
        start_us: int,
        end_us: int,
        att_df: pl.DataFrame,
        gps_df: pl.DataFrame,
    ) -> str:
        notes = []

        alt_range = self._get_alt_range(gps_df, start_us, end_us)
        if alt_range:
            notes.append(f"Alt: {alt_range['min']:.1f}–{alt_range['max']:.1f}m")

        if not att_df.is_empty() and "roll_deg" in att_df.columns:
            att_seg = att_df.filter(
                (pl.col(TIMESTAMP_COL) >= start_us) & (pl.col(TIMESTAMP_COL) <= end_us)
            )
            if not att_seg.is_empty():
                max_roll = float(att_seg["roll_deg"].abs().max())
                max_pitch = float(att_seg["pitch_deg"].abs().max()) if "pitch_deg" in att_seg.columns else 0
                if max_roll > 20 or max_pitch > 20:
                    notes.append(f"Max attitude: roll={max_roll:.0f}° pitch={max_pitch:.0f}°")

        return "; ".join(notes) if notes else ""

    def _build_single_phase(
        self,
        att_df: pl.DataFrame,
        gps_df: pl.DataFrame,
    ) -> list[dict]:
        """Fallback when no MODE data: single UNKNOWN phase."""
        if att_df.is_empty():
            return []
        t_start = int(att_df[TIMESTAMP_COL][0])
        t_end = int(att_df[TIMESTAMP_COL][-1])
        return [{
            "name": "UNKNOWN",
            "start_us": t_start,
            "end_us": t_end,
            "mode_name": "UNKNOWN",
            "notes": "No MODE messages available — cannot segment flight phases",
        }]
