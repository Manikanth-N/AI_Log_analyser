"""[TIMELINE] Flight Timeline Reconstructor Agent."""

from llm.structured import TimelineResult, FlightPhase, TimestampedEvent
from parsers.schema import TIMESTAMP_COL
from .base import BaseAgent


class FlightTimelineAgent(BaseAgent):
    AGENT_NAME = "FlightTimelineAgent"
    AGENT_ROLE = "[TIMELINE] Flight Timeline Reconstructor"

    def run(self, state: dict) -> dict:
        self.emit(state, "Reconstructing flight timeline from stored events and phases...")

        timeline = self.store.read_derived(self.flight_id, "timeline") or {}
        phases_raw = self.store.read_derived(self.flight_id, "phases") or []

        events = timeline.get("events", [])
        critical = timeline.get("critical_events", [])
        event_horizon_us = timeline.get("event_horizon_us")

        # Build typed phase objects
        phases = []
        for p in phases_raw:
            phases.append(FlightPhase(
                name=p["name"],
                start_us=p["start_us"],
                end_us=p["end_us"],
                mode_name=p["mode_name"],
                notes=p.get("notes", ""),
            ))

        # Build key event objects
        key_events = []
        for e in events:
            if e.get("severity") in ("WARNING", "CRITICAL", "FATAL"):
                key_events.append(TimestampedEvent(
                    timestamp_us=e["timestamp_us"],
                    description=e["description"],
                    severity=e.get("severity", "INFO"),
                ))

        # Find crash
        crash_ts = None
        crash_mode = None
        for e in events:
            if e.get("event_type") == "CRASH_DETECTED":
                crash_ts = e["timestamp_us"]
                break

        # Find arm time
        arm_ts = None
        for e in events:
            if e.get("event_type") == "ARM":
                arm_ts = e["timestamp_us"]
                break

        # Compute flight duration
        t_start = phases[0].start_us if phases else 0
        t_end = phases[-1].end_us if phases else 0
        duration_s = (t_end - t_start) / 1e6

        result = TimelineResult(
            phases=phases,
            key_events=key_events,
            event_horizon_us=event_horizon_us,
            crash_detected=crash_ts is not None,
            crash_timestamp_us=crash_ts,
            crash_mode=crash_mode,
            arm_timestamp_us=arm_ts,
            flight_duration_s=duration_s,
        )

        self.emit(
            state,
            f"Timeline: {len(phases)} phases, {len(key_events)} key events, "
            f"duration={duration_s:.1f}s, "
            f"crash={'YES' if crash_ts else 'NO'}"
        )

        if event_horizon_us:
            self.emit(state, f"Event horizon: T+{event_horizon_us/1e6:.1f}s — last normal state")

        state["flight_phases"] = [p.model_dump() for p in phases]
        state["event_timeline"] = [e.model_dump() for e in key_events]
        state.setdefault("agent_findings", {})[self.AGENT_NAME] = {
            "phases": len(phases),
            "key_events": len(key_events),
            "duration_s": duration_s,
            "crash_detected": crash_ts is not None,
            "crash_timestamp_us": crash_ts,
            "arm_timestamp_us": arm_ts,
            "event_horizon_us": event_horizon_us,
        }

        return state
