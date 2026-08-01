"""Pure data contracts shared by the physical controller components."""

from __future__ import annotations

import math
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

    def require_finite(self) -> None:
        numeric = (
            self.timestamp_ms,
            self.wheel_angle_left_rad,
            self.wheel_angle_right_rad,
            self.wheel_speed_left_rad_s,
            self.wheel_speed_right_rad_s,
            self.enc_left,
            self.enc_right,
            self.raw_front_mm,
            self.raw_left_mm,
            self.raw_right_mm,
            self.front_mm,
            self.left_mm,
            self.right_mm,
            self.imu_yaw_deg,
            self.yaw_rate_dps,
            self.accel_forward_mps2,
            self.controller_period_ms,
        )
        if any(
            isinstance(value, bool)
            or not math.isfinite(float(value))
            for value in numeric
        ):
            raise PhysicalDeviceError(
                "SIM_PHYSICS_ERROR",
                "physical device sample contains non-finite data",
            )
        if self.timestamp_ms < 0 or self.controller_period_ms <= 0:
            raise PhysicalDeviceError(
                "SIM_PHYSICS_ERROR",
                "physical sample timing is invalid",
            )
        if not str(self.friction_profile).strip():
            raise PhysicalDeviceError(
                "SIM_PHYSICS_ERROR",
                "physical sample surface is missing",
            )
