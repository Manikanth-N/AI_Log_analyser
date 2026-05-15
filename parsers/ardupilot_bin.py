"""
ArduPilot .BIN parser using pymavlink.
Streams messages from disk without loading entire file into RAM.
Writes per-message-type Parquet files using Polars with chunked writes.
"""

import math
from pathlib import Path
from typing import Callable, Iterator

import polars as pl
import structlog

from config.settings import settings
from .base import AbstractLogParser, ParseError
from .schema import (
    ARDUPILOT_ERR_SUBSYS,
    ARDUPILOT_FLIGHT_MODES_COPTER,
    AutopilotType,
    CRITICAL_MESSAGE_TYPES,
    LogFormat,
    LogMetadata,
    TIMESTAMP_COL,
    VehicleClass,
)

log = structlog.get_logger(__name__)

# Messages we care about and their normalized field mappings.
# Format: { ardupilot_field: canonical_field }
_FIELD_MAPS: dict[str, dict[str, str]] = {
    "ATT": {
        "TimeUS": TIMESTAMP_COL,
        "Roll": "roll_deg",
        "Pitch": "pitch_deg",
        "Yaw": "yaw_deg",
        "RollRate": "roll_rate_deg_s",
        "PitchRate": "pitch_rate_deg_s",
        "YawRate": "yaw_rate_deg_s",
        "DesRoll": "desired_roll_deg",
        "DesPitch": "desired_pitch_deg",
        "DesYaw": "desired_yaw_deg",
    },
    "GPS": {
        "TimeUS": TIMESTAMP_COL,
        "Lat": "lat_deg",
        "Lng": "lng_deg",
        "Alt": "alt_msl_m",
        "RelAlt": "alt_rel_m",
        "HDop": "hdop",
        "VDop": "vdop",
        "NSats": "num_sats",
        "Spd": "gnd_speed_m_s",
        "VZ": "vel_d_m_s",
        "Yaw": "course_deg",
        "SAcc": "speed_acc_m_s",
        "HAcc": "horiz_acc_m",
        "VAcc": "vert_acc_m",
        "Status": "fix_type",
    },
    "IMU": {
        "TimeUS": TIMESTAMP_COL,
        "AccX": "acc_x_m_s2",
        "AccY": "acc_y_m_s2",
        "AccZ": "acc_z_m_s2",
        "GyrX": "gyr_x_rad_s",
        "GyrY": "gyr_y_rad_s",
        "GyrZ": "gyr_z_rad_s",
        "T": "imu_temp_c",
    },
    "IMU2": {
        "TimeUS": TIMESTAMP_COL,
        "AccX": "acc_x_m_s2",
        "AccY": "acc_y_m_s2",
        "AccZ": "acc_z_m_s2",
        "GyrX": "gyr_x_rad_s",
        "GyrY": "gyr_y_rad_s",
        "GyrZ": "gyr_z_rad_s",
        "T": "imu_temp_c",
    },
    "BAT": {
        "TimeUS": TIMESTAMP_COL,
        "Volt": "voltage_v",
        "VoltR": "voltage_v",      # some versions use VoltR
        "Curr": "current_a",
        "CurrTot": "consumed_mah",
        "Res": "remaining_pct",
    },
    "CURR": {  # older ArduPilot versions
        "TimeUS": TIMESTAMP_COL,
        "Volt": "voltage_v",
        "Curr": "current_a",
        "CurrTot": "consumed_mah",
    },
    "RCOU": {
        "TimeUS": TIMESTAMP_COL,
        "C1": "ch1_us",
        "C2": "ch2_us",
        "C3": "ch3_us",
        "C4": "ch4_us",
        "C5": "ch5_us",
        "C6": "ch6_us",
        "C7": "ch7_us",
        "C8": "ch8_us",
    },
    "VIBE": {
        "TimeUS": TIMESTAMP_COL,
        "VibeX": "vibe_x",
        "VibeY": "vibe_y",
        "VibeZ": "vibe_z",
        "Clip0": "clip0",
        "Clip1": "clip1",
        "Clip2": "clip2",
    },
    "BARO": {
        "TimeUS": TIMESTAMP_COL,
        "Alt": "alt_m",
        "Press": "pressure_pa",
        "Temp": "temp_c",
    },
    "MAG": {
        "TimeUS": TIMESTAMP_COL,
        "MagX": "mag_x_ut",
        "MagY": "mag_y_ut",
        "MagZ": "mag_z_ut",
        "OfsX": "offx",
        "OfsY": "offy",
        "OfsZ": "offz",
    },
    "MODE": {
        "TimeUS": TIMESTAMP_COL,
        "Mode": "mode_num",
        "Rsn": "reason",
    },
    "ERR": {
        "TimeUS": TIMESTAMP_COL,
        "Subsys": "subsys",
        "ECode": "ecode",
    },
    "CMD": {
        "TimeUS": TIMESTAMP_COL,
        "CNum": "cmd_num",
        "CId": "cmd_id",
        "Lat": "lat_deg",
        "Lng": "lng_deg",
        "Alt": "alt_m",
        "P1": "param1",
        "P2": "param2",
        "P3": "param3",
        "P4": "param4",
    },
    "PARM": {
        "TimeUS": TIMESTAMP_COL,
        "Name": "name",
        "Value": "value",
    },
    "ESC": {
        "TimeUS": TIMESTAMP_COL,
        "RPM": "rpm",
        "Volt": "voltage_v",
        "Curr": "current_a",
        "Temp": "temp_c",
    },
    # EKF3 messages
    "NKF1": {
        "TimeUS": TIMESTAMP_COL,
        "VN": "vel_n_m_s",
        "VE": "vel_e_m_s",
        "VD": "vel_d_m_s",
        "PN": "pos_n_m",
        "PE": "pos_e_m",
        "PD": "pos_d_m",
    },
    "NKF3": {
        "TimeUS": TIMESTAMP_COL,
        "IVN": "innov_vel_n",
        "IVE": "innov_vel_e",
        "IVD": "innov_vel_d",
        "IPN": "innov_pos_n",
        "IPE": "innov_pos_e",
        "IPD": "innov_pos_d",
        "IMX": "innov_mag_x",
        "IMY": "innov_mag_y",
        "IMZ": "innov_mag_z",
        "IYaw": "innov_heading",
    },
    "NKF4": {
        "TimeUS": TIMESTAMP_COL,
        "SV": "var_ratio_vel",
        "SP": "var_ratio_pos",
        "SH": "var_ratio_hgt",
        "SM": "var_ratio_mag",
        "errRP": "err_roll_pitch",
        "OFN": "offset_n",
        "OFE": "offset_e",
        "FS": "lane",
    },
    "NKF5": {
        "TimeUS": TIMESTAMP_COL,
        "HAGL": "terrain_alt_m",
    },
}

# EKF2 aliases (older firmware)
_FIELD_MAPS["XKF1"] = _FIELD_MAPS["NKF1"]
_FIELD_MAPS["XKF3"] = _FIELD_MAPS["NKF3"]
_FIELD_MAPS["XKF4"] = _FIELD_MAPS["NKF4"]
_FIELD_MAPS["EKF1"] = _FIELD_MAPS["NKF1"]

# All message types to collect
COLLECT_TYPES = set(_FIELD_MAPS.keys())


class ArduPilotBinParser(AbstractLogParser):
    FORMAT = LogFormat.ARDUPILOT_BIN

    def detect(self) -> bool:
        with open(self.file_path, "rb") as f:
            h = f.read(4)
        return len(h) >= 4 and h[0] == 0xA3 and h[1] == 0x95

    def parse_metadata(self) -> LogMetadata:
        from pymavlink import mavutil

        mlog = mavutil.mavlink_connection(
            str(self.file_path),
            dialect="ardupilotmega",
            zero_time_base=True,
        )

        msg_counts: dict[str, int] = {}
        firmware_version = None
        vehicle_type_str = None
        first_ts = None
        last_ts = None

        # Scan whole log for metadata (fast — only reads FMT + PARM + first/last)
        while True:
            msg = mlog.recv_match(blocking=False)
            if msg is None:
                break
            mtype = msg.get_type()
            if mtype == "BAD_DATA":
                continue

            msg_counts[mtype] = msg_counts.get(mtype, 0) + 1

            if hasattr(msg, "TimeUS"):
                ts = msg.TimeUS
                if first_ts is None:
                    first_ts = ts
                last_ts = ts

            if mtype == "MSG" and hasattr(msg, "Message"):
                text = msg.Message
                if "ArduPilot" in text or "Copter" in text or "Plane" in text:
                    firmware_version = text.strip()
                    if "Copter" in text or "Quad" in text or "Hexa" in text:
                        vehicle_type_str = "multirotor"
                    elif "Plane" in text:
                        vehicle_type_str = "fixed_wing"
                    elif "Rover" in text:
                        vehicle_type_str = "rover"

        first_ts = first_ts or 0
        last_ts = last_ts or 0
        duration_us = last_ts - first_ts

        # Compute sample rates
        sample_rates: dict[str, float] = {}
        for mtype, count in msg_counts.items():
            if duration_us > 0:
                sample_rates[mtype] = count / (duration_us / 1_000_000.0)

        missing = [
            t
            for t in CRITICAL_MESSAGE_TYPES["ardupilot"]
            if t not in msg_counts
        ]

        vehicle_map = {
            "multirotor": VehicleClass.MULTIROTOR,
            "fixed_wing": VehicleClass.FIXED_WING,
            "rover": VehicleClass.ROVER,
        }

        return LogMetadata(
            format=LogFormat.ARDUPILOT_BIN,
            autopilot=AutopilotType.ARDUPILOT,
            vehicle_class=vehicle_map.get(vehicle_type_str or "", VehicleClass.UNKNOWN),
            firmware_version=firmware_version,
            firmware_hash=None,
            log_start_us=first_ts,
            log_end_us=last_ts,
            duration_us=duration_us,
            message_types=sorted(msg_counts.keys()),
            message_counts=msg_counts,
            sample_rates_hz=sample_rates,
            missing_critical=missing,
            vehicle_id=None,
            parameter_count=msg_counts.get("PARM", 0),
            file_size_bytes=self.file_path.stat().st_size,
            file_sha256=self.sha256(self.file_path),
        )

    def stream_messages(
        self,
        message_types: list[str] | None = None,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> Iterator[dict]:
        """Stream normalized message dicts without loading full file."""
        from pymavlink import mavutil

        types_to_fetch = list(message_types or COLLECT_TYPES)
        file_size = self.file_path.stat().st_size

        mlog = mavutil.mavlink_connection(
            str(self.file_path),
            dialect="ardupilotmega",
            zero_time_base=True,
        )

        bytes_read = 0
        msg_idx = 0

        while True:
            msg = mlog.recv_match(type=types_to_fetch, blocking=False)
            if msg is None:
                break

            mtype = msg.get_type()
            if mtype == "BAD_DATA":
                continue

            field_map = _FIELD_MAPS.get(mtype)
            if field_map is None:
                continue

            row: dict = {}
            for src_field, dst_field in field_map.items():
                val = getattr(msg, src_field, None)
                if val is not None:
                    row[dst_field] = val

            if TIMESTAMP_COL not in row:
                continue

            # Enrich MODE messages
            if mtype == "MODE" and "mode_num" in row:
                row["mode_name"] = ARDUPILOT_FLIGHT_MODES_COPTER.get(
                    int(row["mode_num"]), f"UNKNOWN_{row['mode_num']}"
                )

            # Enrich ERR messages
            if mtype == "ERR":
                row["subsys_name"] = ARDUPILOT_ERR_SUBSYS.get(
                    int(row.get("subsys", 0)), "UNKNOWN"
                )

            # Tag IMU instance
            if mtype == "IMU2":
                row["instance"] = 1
            elif mtype == "IMU":
                row["instance"] = 0

            row["_type"] = mtype
            msg_idx += 1

            if progress_cb and msg_idx % 10_000 == 0:
                # Approximate byte position
                approx_bytes = int((msg_idx / max(1, msg_idx)) * file_size)
                progress_cb(msg_idx, file_size)

            yield row

    def parse_to_dataframes(
        self,
        output_dir: Path,
        message_types: list[str] | None = None,
        progress_cb: Callable[[float], None] | None = None,
    ) -> dict[str, Path]:
        """
        Stream parse entire log → per-message-type Parquet files.
        Uses row-buffer chunking to avoid unbounded RAM growth.
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        chunk_size = settings.parquet_chunk_rows
        buffers: dict[str, list[dict]] = {}
        writers: dict[str, pl.DataFrame | None] = {}
        output_paths: dict[str, Path] = {}
        total_rows = 0

        log.info("parse_start", file=str(self.file_path), chunk_size=chunk_size)

        for row in self.stream_messages(message_types):
            mtype = row.pop("_type")
            if mtype not in buffers:
                buffers[mtype] = []

            buffers[mtype].append(row)
            total_rows += 1

            # Flush chunk when buffer is full
            if len(buffers[mtype]) >= chunk_size:
                path = self._flush_chunk(mtype, buffers[mtype], output_dir, output_paths)
                output_paths[mtype] = path
                buffers[mtype] = []

            if progress_cb and total_rows % 50_000 == 0:
                progress_cb(total_rows)

        # Flush remaining buffers
        for mtype, buf in buffers.items():
            if buf:
                path = self._flush_chunk(mtype, buf, output_dir, output_paths)
                output_paths[mtype] = path

        log.info("parse_complete", total_rows=total_rows, types=list(output_paths.keys()))
        return output_paths

    def _flush_chunk(
        self,
        mtype: str,
        rows: list[dict],
        output_dir: Path,
        existing_paths: dict[str, Path],
    ) -> Path:
        """Write a chunk of rows to Parquet, appending if file exists."""
        df = pl.DataFrame(rows)

        # Sort by timestamp within chunk
        if TIMESTAMP_COL in df.columns:
            df = df.sort(TIMESTAMP_COL)

        out_path = output_dir / f"{mtype}.parquet"

        if out_path.exists():
            existing = pl.read_parquet(out_path)
            df = pl.concat([existing, df], how="diagonal")
            df = df.sort(TIMESTAMP_COL)

        df.write_parquet(
            out_path,
            compression=settings.parquet_compression,
        )
        return out_path
