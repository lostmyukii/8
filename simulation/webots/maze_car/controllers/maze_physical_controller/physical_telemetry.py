"""Build ordinary protocol telemetry from device evidence only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .physical_types import PhysicalDeviceSample


@dataclass(frozen=True)
class PhysicalTelemetryProvider:
    profile_id: str
    profile_digest: str

    def build(
        self,
        sample: PhysicalDeviceSample,
    ) -> dict[str, Any]:
        return {
            "type": "telemetry",
            "ts_ms": sample.timestamp_ms,
            "simulated": True,
            "simulation_backend": "physical",
            "sensor_source": "webots_device",
            "wheel_angle_left_rad": sample.wheel_angle_left_rad,
            "wheel_angle_right_rad": sample.wheel_angle_right_rad,
            "wheel_speed_left_rad_s": sample.wheel_speed_left_rad_s,
            "wheel_speed_right_rad_s": sample.wheel_speed_right_rad_s,
            "enc_left": sample.enc_left,
            "enc_right": sample.enc_right,
            "raw_front_mm": sample.raw_front_mm,
            "raw_left_mm": sample.raw_left_mm,
            "raw_right_mm": sample.raw_right_mm,
            "front_mm": sample.front_mm,
            "left_mm": sample.left_mm,
            "right_mm": sample.right_mm,
            "imu_available": sample.imu_available,
            "imu_yaw_deg": sample.imu_yaw_deg,
            "yaw_rate_dps": sample.yaw_rate_dps,
            "accel_forward_mps2": sample.accel_forward_mps2,
            "quality_flags": list(sample.quality_flags),
            "controller_period_ms": sample.controller_period_ms,
            "friction_profile": sample.friction_profile,
            "physical_profile_id": self.profile_id,
            "physical_profile_digest": self.profile_digest,
        }
