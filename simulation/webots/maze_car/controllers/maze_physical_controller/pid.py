"""Deterministic velocity PID with fail-closed numeric validation."""

from __future__ import annotations

import math
from dataclasses import dataclass


class ControlInputError(ValueError):
    """Raised when a control calculation receives unsafe numeric input."""


@dataclass(frozen=True)
class PidConfig:
    kp: float
    ki: float
    kd: float
    integral_limit: float
    output_limit: float

    def __post_init__(self) -> None:
        values = (
            self.kp,
            self.ki,
            self.kd,
            self.integral_limit,
            self.output_limit,
        )
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("PID configuration must be finite")
        if self.integral_limit <= 0 or self.output_limit <= 0:
            raise ValueError("PID limits must be positive")


@dataclass(frozen=True)
class PidOutput:
    output: float
    proportional: float
    integral: float
    derivative: float
    limited: bool


class VelocityPid:
    def __init__(self, config: PidConfig) -> None:
        self.config = config
        self._integral = 0.0
        self._previous_measurement: float | None = None
        self._last_output = 0.0

    @property
    def last_output(self) -> float:
        return self._last_output

    def reset(self) -> None:
        self._integral = 0.0
        self._previous_measurement = None
        self._last_output = 0.0

    def update(
        self,
        *,
        setpoint: float,
        measurement: float,
        dt_s: float,
    ) -> PidOutput:
        values = (setpoint, measurement, dt_s)
        if (
            any(not math.isfinite(float(value)) for value in values)
            or dt_s <= 0
        ):
            self.reset()
            raise ControlInputError(
                "PID setpoint, measurement, and positive dt must be finite"
            )

        error = float(setpoint) - float(measurement)
        proportional = self.config.kp * error
        self._integral = _clamp(
            self._integral + error * float(dt_s),
            -self.config.integral_limit,
            self.config.integral_limit,
        )
        integral_term = self.config.ki * self._integral
        derivative = 0.0
        if self._previous_measurement is not None:
            derivative = -self.config.kd * (
                float(measurement) - self._previous_measurement
            ) / float(dt_s)

        raw_output = proportional + integral_term + derivative
        output = _clamp(
            raw_output,
            -self.config.output_limit,
            self.config.output_limit,
        )
        self._previous_measurement = float(measurement)
        self._last_output = output
        return PidOutput(
            output=output,
            proportional=proportional,
            integral=self._integral,
            derivative=derivative,
            limited=output != raw_output,
        )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
