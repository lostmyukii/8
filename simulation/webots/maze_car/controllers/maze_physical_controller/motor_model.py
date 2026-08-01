"""PWM-equivalent dead-zone, gain, and first-order wheel response."""

from __future__ import annotations

import math
from dataclasses import dataclass

from simulation.webots.maze_car.physical_config import MotorConfig

from .pid import ControlInputError


@dataclass(frozen=True)
class MotorModelOutput:
    pwm_left: float
    pwm_right: float
    left_velocity_rad_s: float
    right_velocity_rad_s: float
    left_torque_nm: float
    right_torque_nm: float


class DualMotorModel:
    def __init__(self, config: MotorConfig) -> None:
        self.config = config
        self._left_velocity = 0.0
        self._right_velocity = 0.0

    @property
    def current_velocities(self) -> tuple[float, float]:
        return (self._left_velocity, self._right_velocity)

    def reset(self) -> None:
        self._left_velocity = 0.0
        self._right_velocity = 0.0

    def step(
        self,
        *,
        pwm_left: float,
        pwm_right: float,
        dt_s: float,
    ) -> MotorModelOutput:
        if (
            not all(
                math.isfinite(float(value))
                for value in (pwm_left, pwm_right, dt_s)
            )
            or dt_s <= 0
        ):
            self.reset()
            raise ControlInputError(
                "motor PWM and positive dt must be finite"
            )

        left_pwm = _clamp(float(pwm_left), -1.0, 1.0)
        right_pwm = _clamp(float(pwm_right), -1.0, 1.0)
        left_target = self._target_velocity(
            left_pwm,
            self.config.left_gain,
        )
        right_target = self._target_velocity(
            right_pwm,
            self.config.right_gain,
        )
        alpha = 1.0 - math.exp(
            -float(dt_s) / self.config.response_time_s
        )
        previous_left = self._left_velocity
        previous_right = self._right_velocity
        self._left_velocity += alpha * (
            left_target - self._left_velocity
        )
        self._right_velocity += alpha * (
            right_target - self._right_velocity
        )

        left_torque = self._torque(
            left_target - previous_left
        )
        right_torque = self._torque(
            right_target - previous_right
        )
        return MotorModelOutput(
            pwm_left=left_pwm,
            pwm_right=right_pwm,
            left_velocity_rad_s=self._left_velocity,
            right_velocity_rad_s=self._right_velocity,
            left_torque_nm=left_torque,
            right_torque_nm=right_torque,
        )

    def _target_velocity(
        self,
        pwm: float,
        gain: float,
    ) -> float:
        magnitude = abs(pwm)
        if magnitude <= self.config.pwm_dead_zone:
            return 0.0
        normalized = (
            magnitude - self.config.pwm_dead_zone
        ) / (1.0 - self.config.pwm_dead_zone)
        velocity = (
            math.copysign(normalized, pwm)
            * self.config.max_velocity_rad_s
            * gain
        )
        return _clamp(
            velocity,
            -self.config.max_velocity_rad_s,
            self.config.max_velocity_rad_s,
        )

    def _torque(self, velocity_error: float) -> float:
        ratio = min(
            1.0,
            abs(velocity_error) / self.config.max_velocity_rad_s,
        )
        return self.config.max_torque_nm * ratio


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
