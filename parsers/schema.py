"""
Normalized telemetry schema.
All parsers produce this unified representation regardless of source format.
Timestamps are always in microseconds (int64), monotonically increasing.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AutopilotType(str, Enum):
    ARDUPILOT = "ardupilot"
    PX4 = "px4"
    UNKNOWN = "unknown"


class VehicleClass(str, Enum):
    MULTIROTOR = "multirotor"
    FIXED_WING = "fixed_wing"
    VTOL = "vtol"
    ROVER = "rover"
    SUBMARINE = "submarine"
    UNKNOWN = "unknown"


class LogFormat(str, Enum):
    ARDUPILOT_BIN = "ardupilot_bin"
    PX4_ULOG = "px4_ulog"
    MAVLINK_TLOG = "mavlink_tlog"
    CSV = "csv"
    JSON = "json"


@dataclass
class LogMetadata:
    format: LogFormat
    autopilot: AutopilotType
    vehicle_class: VehicleClass
    firmware_version: Optional[str]
    firmware_hash: Optional[str]
    log_start_us: int           # microseconds from epoch or relative 0
    log_end_us: int
    duration_us: int
    message_types: list[str]    # all present message type names
    message_counts: dict[str, int]
    sample_rates_hz: dict[str, float]
    missing_critical: list[str]  # critical types absent from log
    vehicle_id: Optional[str]
    parameter_count: int
    file_size_bytes: int
    file_sha256: str

    @property
    def duration_seconds(self) -> float:
        return self.duration_us / 1_000_000.0


# ─────────────────────────────────────────────────────────────
# Canonical column names used across all Parquet files.
# Parsers must map native field names to these names.
# ─────────────────────────────────────────────────────────────

# Common to all tables
TIMESTAMP_COL = "timestamp_us"  # int64, microseconds

# ATT / Attitude
ATT_COLS = {
    "roll_deg": float,      # ArduPilot ATT.Roll / PX4 vehicle_attitude derived
    "pitch_deg": float,
    "yaw_deg": float,
    "roll_rate_deg_s": float,
    "pitch_rate_deg_s": float,
    "yaw_rate_deg_s": float,
    "desired_roll_deg": float,
    "desired_pitch_deg": float,
    "desired_yaw_deg": float,
}

# GPS
GPS_COLS = {
    "lat_deg": float,       # degrees (not 1e7 int)
    "lng_deg": float,
    "alt_msl_m": float,     # MSL altitude, meters
    "alt_rel_m": float,     # relative to home, meters
    "hdop": float,
    "vdop": float,
    "num_sats": int,
    "vel_n_m_s": float,     # North velocity m/s
    "vel_e_m_s": float,     # East velocity m/s
    "vel_d_m_s": float,     # Down velocity m/s (positive = descending)
    "gnd_speed_m_s": float,
    "course_deg": float,
    "speed_acc_m_s": float, # speed accuracy estimate
    "horiz_acc_m": float,   # horizontal accuracy
    "vert_acc_m": float,    # vertical accuracy
    "fix_type": int,        # 0=none 1=dead_reckoning 2=2D 3=3D 4=DGPS 5=RTK_float 6=RTK_fixed
    "nsats": int,
}

# IMU
IMU_COLS = {
    "acc_x_m_s2": float,   # acceleration X m/s²
    "acc_y_m_s2": float,
    "acc_z_m_s2": float,
    "gyr_x_rad_s": float,  # gyro X rad/s
    "gyr_y_rad_s": float,
    "gyr_z_rad_s": float,
    "imu_temp_c": float,
    "instance": int,       # 0 = IMU0, 1 = IMU1, etc.
}

# Battery / Power
BAT_COLS = {
    "voltage_v": float,     # pack voltage
    "current_a": float,
    "consumed_mah": float,
    "remaining_pct": float, # 0–100
    "cell_count": int,
    "voltage_per_cell_v": float,
    "instance": int,
}

# RC Output / Motor commands
RCOU_COLS = {
    "ch1_us": float,        # PWM microseconds or normalized 0–1
    "ch2_us": float,
    "ch3_us": float,
    "ch4_us": float,
    "ch5_us": float,
    "ch6_us": float,
    "ch7_us": float,
    "ch8_us": float,
}

# EKF (unified across EKF2/EKF3/ArduPilot/PX4)
EKF_COLS = {
    "vel_n_m_s": float,         # estimated North velocity
    "vel_e_m_s": float,
    "vel_d_m_s": float,
    "pos_n_m": float,           # estimated North position (relative to origin)
    "pos_e_m": float,
    "pos_d_m": float,
    "innov_vel_n": float,       # velocity innovation North
    "innov_vel_e": float,
    "innov_vel_d": float,
    "innov_pos_n": float,
    "innov_pos_e": float,
    "innov_pos_d": float,
    "innov_mag_x": float,       # magnetic innovation
    "innov_mag_y": float,
    "innov_mag_z": float,
    "innov_heading": float,     # heading innovation rad
    "var_ratio_vel": float,     # innovation variance ratio velocity
    "var_ratio_pos": float,     # innovation variance ratio position
    "var_ratio_hgt": float,     # innovation variance ratio height
    "var_ratio_mag": float,
    "offset_n": float,          # GPS position offset correction N
    "offset_e": float,
    "terrain_alt_m": float,
    "lane": int,                # active EKF lane (0 = primary)
}

# Vibration (ArduPilot VIBE / derived from IMU)
VIBE_COLS = {
    "vibe_x": float,    # RMS acceleration X (m/s²)
    "vibe_y": float,
    "vibe_z": float,
    "clip0": int,       # IMU0 clipping events per sample
    "clip1": int,
    "clip2": int,
}

# Barometer
BARO_COLS = {
    "alt_m": float,
    "pressure_pa": float,
    "temp_c": float,
    "instance": int,
}

# Mode changes
MODE_COLS = {
    "mode_num": int,
    "mode_name": str,
    "reason": int,
}

# Error messages
ERR_COLS = {
    "subsys": int,
    "ecode": int,
    "subsys_name": str,
    "ecode_name": str,
}

# Mission commands
CMD_COLS = {
    "cmd_num": int,
    "cmd_id": int,
    "cmd_name": str,
    "param1": float,
    "param2": float,
    "param3": float,
    "param4": float,
    "lat_deg": float,
    "lng_deg": float,
    "alt_m": float,
}

# Parameters
PARAM_COLS = {
    "name": str,
    "value": float,
}

# Magnetometer
MAG_COLS = {
    "mag_x_ut": float,  # microtesla
    "mag_y_ut": float,
    "mag_z_ut": float,
    "offx": float,
    "offy": float,
    "offz": float,
    "instance": int,
}

# ESC telemetry
ESC_COLS = {
    "rpm": float,
    "voltage_v": float,
    "current_a": float,
    "temp_c": float,
    "instance": int,   # motor number 0-based
}


# ─────────────────────────────────────────────────────────────
# ArduPilot subsystem error codes
# ─────────────────────────────────────────────────────────────

ARDUPILOT_ERR_SUBSYS = {
    1: "MAIN",
    2: "RADIO",
    3: "COMPASS",
    4: "OPTFLOW",
    5: "FAILSAFE_RADIO",
    6: "FAILSAFE_BATT",
    7: "FAILSAFE_GPS",
    8: "FAILSAFE_GCS",
    9: "FAILSAFE_FENCE",
    10: "FLIGHT_MODE",
    11: "GPS",
    12: "CRASH_CHECK",
    13: "FLIP",
    14: "AUTOTUNE",
    15: "PARACHUTE",
    16: "EKFCHECK",
    17: "FAILSAFE_EKFINAV",
    18: "BARO",
    19: "CPU",
    20: "RADIO_VERSION",
    21: "ARMING",
    22: "COMPASS_VARIANCE",
    23: "GPS_GLITCH",
    24: "EKF_VARIANCE",
}

ARDUPILOT_FLIGHT_MODES_COPTER = {
    0: "STABILIZE",
    1: "ACRO",
    2: "ALT_HOLD",
    3: "AUTO",
    4: "GUIDED",
    5: "LOITER",
    6: "RTL",
    7: "CIRCLE",
    9: "LAND",
    11: "DRIFT",
    13: "SPORT",
    14: "FLIP",
    15: "AUTOTUNE",
    16: "POSHOLD",
    17: "BRAKE",
    18: "THROW",
    19: "AVOID_ADSB",
    20: "GUIDED_NOGPS",
    21: "SMART_RTL",
    22: "FLOWHOLD",
    23: "FOLLOW",
    24: "ZIGZAG",
    25: "SYSTEMID",
    26: "AUTOROTATE",
    27: "AUTO_RTL",
}

CRITICAL_MESSAGE_TYPES = {
    "ardupilot": ["ATT", "GPS", "IMU", "BAT", "RCOU", "MODE", "ERR"],
    "px4": [
        "vehicle_attitude",
        "vehicle_gps_position",
        "sensor_combined",
        "battery_status",
        "actuator_outputs",
        "vehicle_status",
    ],
}
