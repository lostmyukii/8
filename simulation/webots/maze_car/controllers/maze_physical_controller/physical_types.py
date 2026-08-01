"""Pure data contracts shared by the physical controller components."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


class PhysicalDeviceError(RuntimeError):
    """Fail-closed physical simulation device or command error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = dict(details or {})


@dataclass(frozen=True)
class AppliedMotorCommand:
    left_velocity_rad_s: float
    right_velocity_rad_s: float
    torque_nm: float
    limited: bool


@dataclass(frozen=True)
class PhysicalDeviceSample:
    timestamp_ms: int
    wheel_angle_left_rad: float
    wheel_angle_right_rad: float
    wheel_speed_left_rad_s: float
    wheel_speed_right_rad_s: float
    enc_left: int
    enc_right: int
    raw_front_mm: float
    raw_left_mm: float
    raw_right_mm: float
    front_mm: float
    left_mm: float
    right_mm: float
    imu_available: bool
    imu_yaw_deg: float
    yaw_rate_dps: float
    accel_forward_mps2: float
    quality_flags: tuple[str, ...]
    controller_period_ms: int
    friction_profile: str
