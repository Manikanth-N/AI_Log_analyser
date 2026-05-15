"""
MAVLink .TLOG (telemetry log) parser.
TLOGs contain raw MAVLink packets with wall-clock timestamps.
Extracts key MAVLINK_MSG_ID_* messages and maps to normalized schema.
"""

import struct
from pathlib import Path
from typing import Callable, Iterator

import polars as pl
import structlog

from config.settings import settings
from .base import AbstractLogParser
from .schema import AutopilotType, LogFormat, LogMetadata, TIMESTAMP_COL, VehicleClass

log = structlog.get_logger(__name__)


class MavlinkTlogParser(AbstractLogParser):
    FORMAT = LogFormat.MAVLINK_TLOG

    def detect(self) -> bool:
        with open(self.file_path, "rb") as f:
            header = f.read(8)
        # TLOG: 8-byte timestamp (uint64 big-endian) + MAVLink magic
        if len(header) < 8:
            return False
        magic = header[8:9] if len(header) > 8 else b""
        return header[0:4] != b"ULog"  # not a ULOG = could be TLOG

    def parse_metadata(self) -> LogMetadata:
        from pymavlink import mavutil

        mlog = mavutil.mavlink_connection(str(self.file_path), dialect="ardupilotmega")

        msg_counts: dict[str, int] = {}
        first_ts = None
        last_ts = None

        while True:
            msg = mlog.recv_match(blocking=False)
            if msg is None:
                break
            mtype = msg.get_type()
            if mtype == "BAD_DATA":
                continue
            msg_counts[mtype] = msg_counts.get(mtype, 0) + 1
            ts = getattr(msg, "_timestamp", None)
            if ts:
                if first_ts is None:
                    first_ts = int(ts * 1_000_000)
                last_ts = int(ts * 1_000_000)

        first_ts = first_ts or 0
        last_ts = last_ts or 0
        dur = last_ts - first_ts

        return LogMetadata(
            format=LogFormat.MAVLINK_TLOG,
            autopilot=AutopilotType.ARDUPILOT,
            vehicle_class=VehicleClass.UNKNOWN,
            firmware_version=None,
            firmware_hash=None,
            log_start_us=first_ts,
            log_end_us=last_ts,
            duration_us=dur,
            message_types=sorted(msg_counts.keys()),
            message_counts=msg_counts,
            sample_rates_hz={
                k: v / (dur / 1_000_000.0)
                for k, v in msg_counts.items()
                if dur > 0
            },
            missing_critical=[],
            vehicle_id=None,
            parameter_count=0,
            file_size_bytes=self.file_path.stat().st_size,
            file_sha256=self.sha256(self.file_path),
        )

    def stream_messages(
        self,
        message_types: list[str] | None = None,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> Iterator[dict]:
        from pymavlink import mavutil

        # MAVLink message ID → normalized type + field map
        _MSG_MAP = {
            "ATTITUDE": ("ATT", {
                "roll": "roll_deg",        # rad → deg in post
                "pitch": "pitch_deg",
                "yaw": "yaw_deg",
                "rollspeed": "roll_rate_deg_s",
                "pitchspeed": "pitch_rate_deg_s",
                "yawspeed": "yaw_rate_deg_s",
            }),
            "GPS_RAW_INT": ("GPS", {
                "lat": "lat_deg",          # 1e7 → deg
                "lon": "lng_deg",
                "alt": "alt_msl_m",        # mm → m
                "eph": "hdop",             # cm → divide by 100
                "epv": "vdop",
                "satellites_visible": "num_sats",
                "vel": "gnd_speed_m_s",    # cm/s → m/s
                "fix_type": "fix_type",
            }),
            "BATTERY_STATUS": ("BAT", {
                "voltages": "_voltages",
                "current_battery": "current_a",   # cA → A
                "current_consumed": "consumed_mah",
                "battery_remaining": "remaining_pct",
            }),
            "SYS_STATUS": ("BAT_SYS", {
                "voltage_battery": "voltage_v",   # mV → V
                "current_battery": "current_a",   # cA → A
                "battery_remaining": "remaining_pct",
            }),
            "RC_CHANNELS_RAW": ("RCOU", {
                "chan1_raw": "ch1_us",
                "chan2_raw": "ch2_us",
                "chan3_raw": "ch3_us",
                "chan4_raw": "ch4_us",
            }),
            "HEARTBEAT": ("MODE", {
                "custom_mode": "mode_num",
                "base_mode": "_base_mode",
                "system_status": "_system_status",
            }),
            "STATUSTEXT": ("STATUSTEXT", {
                "text": "text",
                "severity": "severity",
            }),
        }

        mlog = mavutil.mavlink_connection(str(self.file_path), dialect="ardupilotmega")
        import math

        while True:
            msg = mlog.recv_match(blocking=False)
            if msg is None:
                break

            mtype = msg.get_type()
            if mtype not in _MSG_MAP:
                continue

            canonical_type, field_map = _MSG_MAP[mtype]

            ts_sec = getattr(msg, "_timestamp", None)
            if ts_sec is None:
                continue
            ts_us = int(ts_sec * 1_000_000)

            row: dict = {TIMESTAMP_COL: ts_us, "_type": canonical_type}

            for src, dst in field_map.items():
                val = getattr(msg, src, None)
                if val is not None:
                    row[dst] = val

            # Unit conversions
            if mtype == "ATTITUDE":
                for k in ("roll_deg", "pitch_deg", "yaw_deg"):
                    if k in row:
                        row[k] = math.degrees(row[k])
                for k in ("roll_rate_deg_s", "pitch_rate_deg_s", "yaw_rate_deg_s"):
                    if k in row:
                        row[k] = math.degrees(row[k])

            if mtype == "GPS_RAW_INT":
                if "lat_deg" in row:
                    row["lat_deg"] = row["lat_deg"] * 1e-7
                if "lng_deg" in row:
                    row["lng_deg"] = row["lng_deg"] * 1e-7
                if "alt_msl_m" in row:
                    row["alt_msl_m"] = row["alt_msl_m"] * 1e-3
                if "hdop" in row:
                    row["hdop"] = row["hdop"] / 100.0
                if "vdop" in row:
                    row["vdop"] = row["vdop"] / 100.0
                if "gnd_speed_m_s" in row:
                    row["gnd_speed_m_s"] = row["gnd_speed_m_s"] / 100.0

            if mtype == "SYS_STATUS":
                if "voltage_v" in row:
                    row["voltage_v"] = row["voltage_v"] / 1000.0
                if "current_a" in row:
                    row["current_a"] = row["current_a"] / 100.0

            # Clean internal fields
            for k in list(row.keys()):
                if k.startswith("_") and k != "_type":
                    row.pop(k)

            yield row

    def parse_to_dataframes(
        self,
        output_dir: Path,
        message_types: list[str] | None = None,
        progress_cb: Callable[[float], None] | None = None,
    ) -> dict[str, Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        buffers: dict[str, list[dict]] = {}
        output_paths: dict[str, Path] = {}

        for row in self.stream_messages(message_types):
            mtype = row.pop("_type")
            buffers.setdefault(mtype, []).append(row)

        for mtype, rows in buffers.items():
            if not rows:
                continue
            df = pl.DataFrame(rows).sort(TIMESTAMP_COL)
            out_path = output_dir / f"{mtype}.parquet"
            df.write_parquet(out_path, compression=settings.parquet_compression)
            output_paths[mtype] = out_path

        return output_paths
