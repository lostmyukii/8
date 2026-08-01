"""The only module that directly adapts Webots physical devices."""

from __future__ import annotations

import math
import random
from typing import Any

from simulation.webots.maze_car.physical_config import PhysicalProfile

from .physical_types import (
    AppliedMotorCommand,
    PhysicalDeviceError,
    PhysicalDeviceSample,
)


DEVICE_NAMES = (
    "left wheel motor",
    "right wheel motor",
    "left wheel encoder",
    "right wheel encoder",
    "tof front",
    "tof left",
    "tof right",
    "imu",
    "gyro",
    "accelerometer",
)
_MOTOR_NAMES = DEVICE_NAMES[:2]
_SENSOR_NAMES = DEVICE_NAMES[2:]
_TOF_NAMES = {
    "front": "tof front",
    "left": "tof left",
    "right": "tof right",
}
_TOF_FILTER_ALPHA = 0.35


class PhysicalDeviceAdapter:
    """Validate, sample, and command one physical Webots robot.

    The adapter intentionally accepts a duck-typed Robot so its conversion and
    safety behavior can be tested without importing the Webots ``controller``
    package.
    """

    def __init__(
        self,
        robot: Any,
        profile: PhysicalProfile,
        *,
        sensor_noise_enabled: bool = True,
        sensor_dropout_enabled: bool = True,
    ) -> None:
        self._robot = robot
        self.profile = profile
        self.sensor_noise_enabled = bool(sensor_noise_enabled)
        self.sensor_dropout_enabled = bool(sensor_dropout_enabled)
        self._rng = random.Random(profile.random_seed)
        self._devices: dict[str, Any] = {}
        self._last_timestamp_ms: int | None = None
        self._last_angles: tuple[float, float] | None = None
        self._encoder_origins: tuple[float, float] | None = None
        self._tof_filtered: dict[str, float] = {}

        self._bind_devices()

    def _bind_devices(self) -> None:
        self._devices.clear()
        for name in DEVICE_NAMES:
            try:
                device = self._robot.getDevice(name)
            except Exception:
                device = None
            if device is not None:
                self._devices[name] = device

        missing = next(
            (name for name in DEVICE_NAMES if name not in self._devices),
            None,
        )
        if missing is not None:
            self.safe_stop()
            raise PhysicalDeviceError(
                "SIM_DEVICE_MISSING",
                f"required Webots device is missing: {missing}",
                details={"device": missing},
            )

        try:
            for name in _MOTOR_NAMES:
                motor = self._devices[name]
                motor.setPosition(float("inf"))
                motor.setAvailableTorque(
                    self.profile.motor.max_torque_nm
                )
                motor.setVelocity(0.0)
            for name in _SENSOR_NAMES:
                self._devices[name].enable(
                    self.profile.runtime.basic_time_step_ms
                )
        except Exception as exc:
            self.safe_stop()
            raise PhysicalDeviceError(
                "SIM_DEVICE_INIT_FAILED",
                f"cannot initialize Webots devices: {exc}",
            ) from exc

    def reset(self) -> None:
        self.safe_stop()
        self._last_timestamp_ms = None
        self._last_angles = None
        self._encoder_origins = None
        self._tof_filtered.clear()
        self._rng.seed(self.profile.random_seed)

    def apply_profile(self, profile: PhysicalProfile) -> None:
        """Switch one verified immutable profile at a stopped reset boundary."""

        self.safe_stop()
        self.profile = profile
        # Changing a PROTO field can regenerate its Webots device nodes.
        # Never keep motor/sensor handles across that reset boundary.
        self._bind_devices()
        self.reset()

    def safe_stop(self) -> None:
        for name in _MOTOR_NAMES:
            motor = self._devices.get(name)
            if motor is None:
                continue
            try:
                motor.setVelocity(0.0)
            except Exception:
                pass

    def command_wheels(
        self,
        *,
        left_velocity_rad_s: float,
        right_velocity_rad_s: float,
        torque_nm: float | None = None,
    ) -> AppliedMotorCommand:
        values = [left_velocity_rad_s, right_velocity_rad_s]
        if torque_nm is not None:
            values.append(torque_nm)
        if any(
            isinstance(value, bool) or not math.isfinite(float(value))
            for value in values
        ):
            self.safe_stop()
            raise PhysicalDeviceError(
                "SIM_INVALID_MOTOR_COMMAND",
                "motor velocity and torque commands must be finite numbers",
            )

        velocity_limit = self.profile.motor.max_velocity_rad_s
        torque_limit = self.profile.motor.max_torque_nm
        requested_torque = (
            torque_limit if torque_nm is None else float(torque_nm)
        )
        left = _clamp(
            float(left_velocity_rad_s),
            -velocity_limit,
            velocity_limit,
        )
        right = _clamp(
            float(right_velocity_rad_s),
            -velocity_limit,
            velocity_limit,
        )
        torque = _clamp(requested_torque, 0.0, torque_limit)
        limited = (
            left != float(left_velocity_rad_s)
            or right != float(right_velocity_rad_s)
            or torque != requested_torque
        )
        try:
            for name, velocity in zip(_MOTOR_NAMES, (left, right)):
                motor = self._devices[name]
                motor.setAvailableTorque(torque)
                motor.setVelocity(velocity)
        except Exception as exc:
            self.safe_stop()
            raise PhysicalDeviceError(
                "SIM_MOTOR_COMMAND_FAILED",
                f"cannot apply wheel command: {exc}",
            ) from exc
        return AppliedMotorCommand(
            left_velocity_rad_s=left,
            right_velocity_rad_s=right,
            torque_nm=torque,
            limited=limited,
        )

    def sample(self, *, timestamp_ms: int) -> PhysicalDeviceSample:
        try:
            left_angle = float(
                self._devices["left wheel encoder"].getValue()
            )
            right_angle = float(
                self._devices["right wheel encoder"].getValue()
            )
            imu = tuple(
                float(value)
                for value in self._devices["imu"].getRollPitchYaw()
            )
            gyro = tuple(
                float(value)
                for value in self._devices["gyro"].getValues()
            )
            acceleration = tuple(
                float(value)
                for value in self._devices["accelerometer"].getValues()
            )
        except Exception as exc:
            self.safe_stop()
            raise PhysicalDeviceError(
                "SIM_PHYSICS_ERROR",
                f"cannot read Webots physical devices: {exc}",
            ) from exc

        numeric_values = (
            left_angle,
            right_angle,
            *imu,
            *gyro,
            *acceleration,
        )
        if (
            len(imu) != 3
            or len(gyro) != 3
            or len(acceleration) != 3
            or any(not math.isfinite(value) for value in numeric_values)
        ):
            self.safe_stop()
            raise PhysicalDeviceError(
                "SIM_PHYSICS_ERROR",
                "encoder or inertial device returned non-finite data",
            )

        timestamp = int(timestamp_ms)
        if self._encoder_origins is None:
            self._encoder_origins = (left_angle, right_angle)
        left_ticks = self._angle_to_ticks(
            left_angle - self._encoder_origins[0]
        )
        right_ticks = self._angle_to_ticks(
            right_angle - self._encoder_origins[1]
        )

        left_speed = 0.0
        right_speed = 0.0
        if self._last_angles is not None and self._last_timestamp_ms is not None:
            delta_ms = timestamp - self._last_timestamp_ms
            if delta_ms > 0:
                delta_s = delta_ms / 1000.0
                left_speed = (
                    left_angle - self._last_angles[0]
                ) / delta_s
                right_speed = (
                    right_angle - self._last_angles[1]
                ) / delta_s

        quality_flags: set[str] = set()
        if (
            self.sensor_noise_enabled
            and self.profile.tof.noise_std_mm > 0
        ):
            quality_flags.add("tof_noise_enabled")
        if (
            self.sensor_dropout_enabled
            and self.profile.tof.dropout_rate > 0
        ):
            quality_flags.add("tof_dropout_enabled")
        raw_tof: dict[str, float] = {}
        filtered_tof: dict[str, float] = {}
        for direction, name in _TOF_NAMES.items():
            raw_mm, filtered_mm, flags = self._sample_tof(
                direction,
                self._devices[name],
            )
            raw_tof[direction] = raw_mm
            filtered_tof[direction] = filtered_mm
            quality_flags.update(flags)

        yaw_deg = (
            -math.degrees(imu[2])
            + self._rng.gauss(0.0, self.profile.imu.yaw_noise_std_deg)
        ) % 360.0
        yaw_rate_dps = (
            -math.degrees(gyro[1])
            + self._rng.gauss(0.0, self.profile.imu.gyro_noise_std_dps)
        )
        accel_forward_mps2 = (
            -acceleration[2]
            + self._rng.gauss(
                0.0,
                self.profile.imu.accel_noise_std_mps2,
            )
        )
        if not all(
            math.isfinite(value)
            for value in (yaw_deg, yaw_rate_dps, accel_forward_mps2)
        ):
            self.safe_stop()
            raise PhysicalDeviceError(
                "SIM_PHYSICS_ERROR",
                "simulated inertial noise produced non-finite data",
            )

        self._last_timestamp_ms = timestamp
        self._last_angles = (left_angle, right_angle)
        return PhysicalDeviceSample(
            timestamp_ms=timestamp,
            wheel_angle_left_rad=left_angle,
            wheel_angle_right_rad=right_angle,
            wheel_speed_left_rad_s=left_speed,
            wheel_speed_right_rad_s=right_speed,
            enc_left=left_ticks,
            enc_right=right_ticks,
            raw_front_mm=raw_tof["front"],
            raw_left_mm=raw_tof["left"],
            raw_right_mm=raw_tof["right"],
            front_mm=filtered_tof["front"],
            left_mm=filtered_tof["left"],
            right_mm=filtered_tof["right"],
            imu_available=True,
            imu_yaw_deg=yaw_deg,
            yaw_rate_dps=yaw_rate_dps,
            accel_forward_mps2=accel_forward_mps2,
            quality_flags=tuple(sorted(quality_flags)),
            controller_period_ms=self.profile.runtime.basic_time_step_ms,
            friction_profile=self.profile.surface.profile,
        )

    def _angle_to_ticks(self, angle_rad: float) -> int:
        raw_ticks = (
            float(angle_rad)
            * self.profile.encoder.ticks_per_revolution
            / (2.0 * math.pi)
        )
        if self.profile.encoder.quantization_enabled:
            raw_ticks = round(raw_ticks)
        retained = 1.0 - self.profile.encoder.missed_pulse_rate
        return int(round(raw_ticks * retained))

    def _sample_tof(
        self,
        direction: str,
        device: Any,
    ) -> tuple[float, float, set[str]]:
        flags: set[str] = set()
        try:
            raw_m = float(device.getValue())
        except Exception as exc:
            self.safe_stop()
            raise PhysicalDeviceError(
                "SIM_PHYSICS_ERROR",
                f"cannot read {direction} ToF: {exc}",
            ) from exc
        raw_mm = raw_m * 1000.0
        minimum_mm = self.profile.tof.min_range_m * 1000.0
        maximum_mm = self.profile.tof.max_range_m * 1000.0
        if not math.isfinite(raw_mm):
            raw_mm = maximum_mm
            flags.add(f"tof_{direction}_non_finite")

        bounded = _clamp(raw_mm, minimum_mm, maximum_mm)
        if bounded != raw_mm:
            side = "low" if raw_mm < minimum_mm else "high"
            flags.add(f"tof_{direction}_clamped_{side}")

        if (
            self.sensor_dropout_enabled
            and self._rng.random() < self.profile.tof.dropout_rate
        ):
            flags.add(f"tof_{direction}_dropout")
            filtered = self._tof_filtered.get(direction, maximum_mm)
            self._tof_filtered[direction] = filtered
            return (
                round(raw_mm, 6),
                round(filtered, 6),
                flags,
            )

        noise_std_mm = (
            self.profile.tof.noise_std_mm
            if self.sensor_noise_enabled
            else 0.0
        )
        noisy = bounded + self._rng.gauss(0.0, noise_std_mm)
        noisy = _clamp(noisy, minimum_mm, maximum_mm)
        previous = self._tof_filtered.get(direction)
        if previous is None:
            filtered = noisy
        else:
            filtered = (
                _TOF_FILTER_ALPHA * noisy
                + (1.0 - _TOF_FILTER_ALPHA) * previous
            )
            flags.add("tof_filtered")
        self._tof_filtered[direction] = filtered
        return (
            round(raw_mm, 6),
            round(filtered, 6),
            flags,
        )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
