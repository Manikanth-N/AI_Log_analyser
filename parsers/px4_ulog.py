"""
PX4 .ULOG parser using pyulog.
Maps PX4 topic names and fields to the normalized schema.
"""

from pathlib import Path
from typing import Callable, Iterator

import numpy as np
import polars as pl
import structlog

from config.settings import settings
from .base import AbstractLogParser, ParseError
from .schema import (
    AutopilotType,
    CRITICAL_MESSAGE_TYPES,
    LogFormat,
    LogMetadata,
    TIMESTAMP_COL,
    VehicleClass,
)

log = structlog.get_logger(__name__)

# PX4 topic → (canonical_type_name, {px4_field: canonical_field})
_PX4_TOPIC_MAPS: dict[str, tuple[str, dict[str, str]]] = {
    "vehicle_attitude": ("ATT", {
        "timestamp": TIMESTAMP_COL,
        # PX4 uses quaternion — we compute euler
        "q[0]": "_q0",
        "q[1]": "_q1",
        "q[2]": "_q2",
        "q[3]": "_q3",
        "rollspeed": "roll_rate_deg_s",   # will convert rad→deg
        "pitchspeed": "pitch_rate_deg_s",
        "yawspeed": "yaw_rate_deg_s",
    }),
    "vehicle_attitude_setpoint": ("ATT_SP", {
        "timestamp": TIMESTAMP_COL,
        "roll_body": "desired_roll_deg",
        "pitch_body": "desired_pitch_deg",
        "yaw_body": "desired_yaw_deg",
    }),
    "vehicle_gps_position": ("GPS", {
        "timestamp": TIMESTAMP_COL,
        "lat": "lat_deg",         # stored as int32 1e7, will convert
        "lon": "lng_deg",
        "alt": "alt_msl_m",       # stored as int32 mm, will convert
        "alt_ellipsoid": "_alt_ellipsoid",
        "hdop": "hdop",           # stored as uint16 * 100
        "vdop": "vdop",
        "satellites_used": "num_sats",
        "vel_n_m_s": "vel_n_m_s",
        "vel_e_m_s": "vel_e_m_s",
        "vel_d_m_s": "vel_d_m_s",
        "vel_ned_valid": "_vel_ned_valid",
        "s_variance_m_s": "speed_acc_m_s",
        "eph": "horiz_acc_m",
        "epv": "vert_acc_m",
        "fix_type": "fix_type",
    }),
    "sensor_combined": ("IMU", {
        "timestamp": TIMESTAMP_COL,
        "accelerometer_m_s2[0]": "acc_x_m_s2",
        "accelerometer_m_s2[1]": "acc_y_m_s2",
        "accelerometer_m_s2[2]": "acc_z_m_s2",
        "gyro_rad[0]": "gyr_x_rad_s",
        "gyro_rad[1]": "gyr_y_rad_s",
        "gyro_rad[2]": "gyr_z_rad_s",
    }),
    "battery_status": ("BAT", {
        "timestamp": TIMESTAMP_COL,
        "voltage_v": "voltage_v",
        "current_a": "current_a",
        "discharged_mah": "consumed_mah",
        "remaining": "remaining_pct",  # 0.0–1.0, will scale to 0–100
        "cell_count": "cell_count",
    }),
    "actuator_outputs": ("RCOU", {
        "timestamp": TIMESTAMP_COL,
        "output[0]": "ch1_us",
        "output[1]": "ch2_us",
        "output[2]": "ch3_us",
        "output[3]": "ch4_us",
        "output[4]": "ch5_us",
        "output[5]": "ch6_us",
        "output[6]": "ch7_us",
        "output[7]": "ch8_us",
    }),
    "vehicle_status": ("MODE", {
        "timestamp": TIMESTAMP_COL,
        "nav_state": "mode_num",
        "arming_state": "_arming_state",
    }),
    "estimator_status": ("NKF4", {
        "timestamp": TIMESTAMP_COL,
        "vel_test_ratio": "var_ratio_vel",
        "pos_test_ratio": "var_ratio_pos",
        "hgt_test_ratio": "var_ratio_hgt",
        "mag_test_ratio": "var_ratio_mag",
    }),
    "estimator_innovations": ("NKF3", {
        "timestamp": TIMESTAMP_COL,
        "vel_pos_innov[0]": "innov_vel_n",
        "vel_pos_innov[1]": "innov_vel_e",
        "vel_pos_innov[2]": "innov_vel_d",
        "vel_pos_innov[3]": "innov_pos_n",
        "vel_pos_innov[4]": "innov_pos_e",
        "vel_pos_innov[5]": "innov_pos_d",
        "mag_innov[0]": "innov_mag_x",
        "mag_innov[1]": "innov_mag_y",
        "mag_innov[2]": "innov_mag_z",
        "heading_innov": "innov_heading",
    }),
    "vehicle_local_position": ("NKF1", {
        "timestamp": TIMESTAMP_COL,
        "vx": "vel_n_m_s",
        "vy": "vel_e_m_s",
        "vz": "vel_d_m_s",
        "x": "pos_n_m",
        "y": "pos_e_m",
        "z": "pos_d_m",
    }),
}

# PX4 nav state codes
PX4_NAV_STATES = {
    0: "MANUAL",
    1: "ALTCTL",
    2: "POSCTL",
    3: "AUTO_MISSION",
    4: "AUTO_LOITER",
    5: "AUTO_RTL",
    6: "ACRO",
    10: "STAB",
    14: "AUTO_TAKEOFF",
    15: "AUTO_LAND",
    17: "AUTO_FOLLOW_TARGET",
    18: "AUTO_PRECLAND",
}


class PX4UlogParser(AbstractLogParser):
    FORMAT = LogFormat.PX4_ULOG

    def detect(self) -> bool:
        with open(self.file_path, "rb") as f:
            return f.read(5) == b"ULog\x01"

    def _load_ulog(self):
        from pyulog import ULog
        return ULog(str(self.file_path))

    def parse_metadata(self) -> LogMetadata:
        ulog = self._load_ulog()

        msg_counts = {d.name: len(d.data["timestamp"]) for d in ulog.data_list}
        duration_us = ulog.last_timestamp - ulog.start_timestamp
        sample_rates = {
            name: count / (duration_us / 1_000_000.0)
            for name, count in msg_counts.items()
            if duration_us > 0
        }

        fw_version = None
        for info_key, info_val in ulog.msg_info_dict.items():
            if "version" in info_key.lower():
                fw_version = str(info_val)
                break

        missing = [
            t for t in CRITICAL_MESSAGE_TYPES["px4"]
            if t not in msg_counts
        ]

        return LogMetadata(
            format=LogFormat.PX4_ULOG,
            autopilot=AutopilotType.PX4,
            vehicle_class=VehicleClass.MULTIROTOR,  # TODO: detect from params
            firmware_version=fw_version,
            firmware_hash=None,
            log_start_us=ulog.start_timestamp,
            log_end_us=ulog.last_timestamp,
            duration_us=duration_us,
            message_types=sorted(msg_counts.keys()),
            message_counts=msg_counts,
            sample_rates_hz=sample_rates,
            missing_critical=missing,
            vehicle_id=None,
            parameter_count=len(ulog.initial_parameters),
            file_size_bytes=self.file_path.stat().st_size,
            file_sha256=self.sha256(self.file_path),
        )

    def stream_messages(
        self,
        message_types: list[str] | None = None,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> Iterator[dict]:
        ulog = self._load_ulog()

        for dataset in ulog.data_list:
            topic = dataset.name
            if topic not in _PX4_TOPIC_MAPS:
                continue

            canonical_type, field_map = _PX4_TOPIC_MAPS[topic]
            data = dataset.data

            n_rows = len(data.get("timestamp", []))
            for i in range(n_rows):
                row: dict = {"_type": canonical_type}

                for px4_field, canonical_field in field_map.items():
                    if px4_field in data:
                        row[canonical_field] = float(data[px4_field][i])

                if TIMESTAMP_COL not in row:
                    continue

                # PX4-specific unit conversions
                if topic == "vehicle_gps_position":
                    if "lat_deg" in row:
                        row["lat_deg"] = row["lat_deg"] * 1e-7
                    if "lng_deg" in row:
                        row["lng_deg"] = row["lng_deg"] * 1e-7
                    if "alt_msl_m" in row:
                        row["alt_msl_m"] = row["alt_msl_m"] * 1e-3  # mm → m

                if topic == "vehicle_attitude":
                    q0 = row.pop("_q0", 1.0)
                    q1 = row.pop("_q1", 0.0)
                    q2 = row.pop("_q2", 0.0)
                    q3 = row.pop("_q3", 0.0)
                    roll, pitch, yaw = _quat_to_euler(q0, q1, q2, q3)
                    row["roll_deg"] = np.degrees(roll)
                    row["pitch_deg"] = np.degrees(pitch)
                    row["yaw_deg"] = np.degrees(yaw)
                    # Convert body rates rad → deg
                    for k in ("roll_rate_deg_s", "pitch_rate_deg_s", "yaw_rate_deg_s"):
                        if k in row:
                            row[k] = np.degrees(row[k])

                if topic == "battery_status" and "remaining_pct" in row:
                    row["remaining_pct"] = row["remaining_pct"] * 100.0

                if topic == "vehicle_status":
                    row["mode_name"] = PX4_NAV_STATES.get(
                        int(row.get("mode_num", 0)), "UNKNOWN"
                    )
                    row.pop("_arming_state", None)

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
        total_rows = 0

        for row in self.stream_messages(message_types):
            mtype = row.pop("_type")
            buffers.setdefault(mtype, []).append(row)
            total_rows += 1
            if progress_cb and total_rows % 50_000 == 0:
                progress_cb(float(total_rows))

        for mtype, rows in buffers.items():
            if not rows:
                continue
            df = pl.DataFrame(rows).sort(TIMESTAMP_COL)
            out_path = output_dir / f"{mtype}.parquet"
            df.write_parquet(out_path, compression=settings.parquet_compression)
            output_paths[mtype] = out_path

        return output_paths


def _quat_to_euler(q0: float, q1: float, q2: float, q3: float) -> tuple[float, float, float]:
    """Convert quaternion (w, x, y, z) to Euler angles (roll, pitch, yaw) in radians."""
    sinr_cosp = 2.0 * (q0 * q1 + q2 * q3)
    cosr_cosp = 1.0 - 2.0 * (q1 * q1 + q2 * q2)
    roll = np.arctan2(sinr_cosp, cosr_cosp)

    sinp = 2.0 * (q0 * q2 - q3 * q1)
    sinp = np.clip(sinp, -1.0, 1.0)
    pitch = np.arcsin(sinp)

    siny_cosp = 2.0 * (q0 * q3 + q1 * q2)
    cosy_cosp = 1.0 - 2.0 * (q2 * q2 + q3 * q3)
    yaw = np.arctan2(siny_cosp, cosy_cosp)

    return roll, pitch, yaw
