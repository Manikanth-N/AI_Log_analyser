from .ekf_rules import ALL_EKF_RULES
from .gps_rules import ALL_GPS_RULES
from .power_rules import ALL_POWER_RULES
from .vibration_rules import ALL_VIBRATION_RULES
from .motor_rules import ALL_MOTOR_RULES
from .failsafe_rules import ALL_FAILSAFE_RULES
from .base_rule import BaseRule, RuleAnomaly, Severity

ALL_RULES: list[BaseRule] = (
    ALL_EKF_RULES +
    ALL_GPS_RULES +
    ALL_POWER_RULES +
    ALL_VIBRATION_RULES +
    ALL_MOTOR_RULES +
    ALL_FAILSAFE_RULES
)

__all__ = [
    "ALL_RULES",
    "ALL_EKF_RULES",
    "ALL_GPS_RULES",
    "ALL_POWER_RULES",
    "ALL_VIBRATION_RULES",
    "ALL_MOTOR_RULES",
    "ALL_FAILSAFE_RULES",
    "BaseRule",
    "RuleAnomaly",
    "Severity",
]
