"""Deterministic encoder, IMU, wall, and grid-anchor pose estimator."""

from __future__ import annotations

import math
from collections.abc import Iterable

from .pose_types import (
    HEADING_YAW_DEG,
    HEADINGS,
    PoseEstimate,
    PoseFusionConfig,
    PoseObservation,
    WallConstraint,
    angle_delta_deg,
    normalize_yaw,
)


class PoseFusion:
    """Small diagonal-covariance estimator suitable for fake-client tests."""

    def __init__(
        self,
        *,
        config: PoseFusionConfig,
        initial_cell: tuple[int, int] = (0, 0),
        initial_heading: str = "N",
    ) -> None:
        if initial_heading not in HEADINGS:
            raise ValueError("initial_heading must be N, E, S, or W")
        self.config = config
        self._grid_cell = (int(initial_cell[0]), int(initial_cell[1]))
        self._heading = initial_heading
        self._x_mm, self._y_mm = self._cell_center(self._grid_cell)
        self._yaw_deg = HEADING_YAW_DEG[initial_heading]
        self._covariance = [625.0, 625.0, 25.0]
        self._last_observation: PoseObservation | None = None
        self._speed_mm_s = 0.0
        self._angular_velocity_dps = 0.0
        self._wall_residual_mm: float | None = None
        self._continuous_heading_valid = False
        self._quality_flags: tuple[str, ...] = ("uninitialized",)
        self._correction_source = "initialized"

    def update(
        self,
        observation: PoseObservation,
        *,
        wall_constraints: Iterable[WallConstraint] = (),
    ) -> PoseEstimate:
        flags = set(observation.quality_flags)
        source = "encoder_prediction"
        self._wall_residual_mm = None
        previous = self._last_observation
        dt_s = self._delta_seconds(previous, observation)

        if previous is not None:
            left_mm = (
                observation.enc_left - previous.enc_left
            ) * self.config.mm_per_tick
            right_mm = (
                observation.enc_right - previous.enc_right
            ) * self.config.mm_per_tick
            distance_mm = (left_mm + right_mm) / 2.0
            yaw_delta_deg = -(
                (observation.enc_right - previous.enc_right)
                - (observation.enc_left - previous.enc_left)
            ) * self.config.yaw_deg_per_differential_tick
            prior_yaw = self._yaw_deg
            predicted_yaw = normalize_yaw(prior_yaw + yaw_delta_deg)
            mid_yaw_rad = math.radians(
                normalize_yaw(prior_yaw + yaw_delta_deg / 2.0)
            )
            self._x_mm += math.sin(mid_yaw_rad) * distance_mm
            y_sign = -1.0 if self.config.y_axis_down else 1.0
            self._y_mm += (
                y_sign * math.cos(mid_yaw_rad) * distance_mm
            )
            self._yaw_deg = predicted_yaw
            self._covariance[0] += 4.0 + abs(distance_mm) * 0.4
            self._covariance[1] += 4.0 + abs(distance_mm) * 0.4
            self._covariance[2] += 1.0 + abs(yaw_delta_deg) * 0.12
            if dt_s > 0:
                self._speed_mm_s = distance_mm / dt_s
                self._angular_velocity_dps = yaw_delta_deg / dt_s
        else:
            self._speed_mm_s = 0.0
            self._angular_velocity_dps = 0.0

        if observation.imu_available:
            imu_source = self._apply_imu(
                observation,
                previous=previous,
                dt_s=dt_s,
            )
            if imu_source is not None:
                source = imu_source
                self._continuous_heading_valid = True
            else:
                flags.add("imu_invalid")
                self._continuous_heading_valid = False
        else:
            flags.add("imu_unavailable")
            self._continuous_heading_valid = False

        wall_sources = []
        residuals = []
        for constraint in wall_constraints:
            applied, residual = self._apply_wall_constraint(constraint)
            if residual is not None:
                residuals.append(abs(residual))
            if applied:
                wall_sources.append(f"tof_wall_{constraint.direction}")
            elif residual is not None:
                flags.add("wall_outlier")
        if residuals:
            self._wall_residual_mm = round(
                sum(residuals) / len(residuals),
                3,
            )
        if wall_sources:
            source = "+".join(wall_sources)

        self._last_observation = observation
        self._quality_flags = tuple(sorted(flags))
        self._correction_source = source
        return self.estimate()

    def anchor_grid(
        self,
        cell: tuple[int, int],
        heading: str,
        *,
        source: str = "grid_action_done",
    ) -> PoseEstimate:
        if heading not in HEADINGS:
            raise ValueError("heading must be N, E, S, or W")
        target_x, target_y = self._cell_center(cell)
        self._x_mm, self._covariance[0] = self._scalar_update(
            self._x_mm,
            self._covariance[0],
            target_x,
            9.0,
        )
        self._y_mm, self._covariance[1] = self._scalar_update(
            self._y_mm,
            self._covariance[1],
            target_y,
            9.0,
        )
        self._yaw_deg, self._covariance[2] = self._angle_update(
            self._yaw_deg,
            self._covariance[2],
            HEADING_YAW_DEG[heading],
            4.0,
        )
        self._grid_cell = (int(cell[0]), int(cell[1]))
        self._heading = heading
        self._correction_source = source
        return self.estimate()

    def estimate(self) -> PoseEstimate:
        confidence = self._confidence()
        return PoseEstimate(
            grid_cell=self._grid_cell,
            heading=self._heading,
            x_mm=round(self._x_mm, 3),
            y_mm=round(self._y_mm, 3),
            yaw_deg=round(normalize_yaw(self._yaw_deg), 3),
            speed_mm_s=round(self._speed_mm_s, 3),
            angular_velocity_dps=round(
                self._angular_velocity_dps,
                3,
            ),
            covariance=tuple(
                round(max(0.0, value), 3)
                for value in self._covariance
            ),
            confidence=round(confidence, 4),
            correction_source=self._correction_source,
            wall_residual_mm=self._wall_residual_mm,
            continuous_heading_valid=self._continuous_heading_valid,
            quality_flags=self._quality_flags,
        )

    def _apply_imu(
        self,
        observation: PoseObservation,
        *,
        previous: PoseObservation | None,
        dt_s: float,
    ) -> str | None:
        if observation.imu_yaw_deg is not None:
            self._yaw_deg, self._covariance[2] = self._angle_update(
                self._yaw_deg,
                self._covariance[2],
                observation.imu_yaw_deg,
                4.0,
            )
            if observation.yaw_rate_dps is not None:
                self._angular_velocity_dps = observation.yaw_rate_dps
            return "imu_yaw"
        if (
            observation.yaw_rate_dps is not None
            and previous is not None
            and dt_s > 0
        ):
            imu_prediction = normalize_yaw(
                self._yaw_deg + observation.yaw_rate_dps * dt_s
            )
            self._yaw_deg, self._covariance[2] = self._angle_update(
                self._yaw_deg,
                self._covariance[2],
                imu_prediction,
                9.0,
            )
            self._angular_velocity_dps = observation.yaw_rate_dps
            return "imu_yaw_rate"
        return None

    def _apply_wall_constraint(
        self,
        constraint: WallConstraint,
    ) -> tuple[bool, float | None]:
        axis, measurement = constraint.position_measurement()
        current = self._x_mm if axis == "x" else self._y_mm
        variance_index = 0 if axis == "x" else 1
        residual = measurement - current
        limit = (
            float(constraint.max_residual_mm)
            if constraint.max_residual_mm is not None
            else float(self.config.wall_outlier_limit_mm)
        )
        if abs(residual) > limit:
            return False, residual
        updated, variance = self._scalar_update(
            current,
            self._covariance[variance_index],
            measurement,
            float(constraint.variance_mm2),
        )
        if axis == "x":
            self._x_mm = updated
        else:
            self._y_mm = updated
        self._covariance[variance_index] = variance
        return True, residual

    def _cell_center(
        self,
        cell: tuple[int, int],
    ) -> tuple[float, float]:
        return (
            (float(cell[0]) + 0.5) * self.config.cell_width_mm,
            (float(cell[1]) + 0.5) * self.config.cell_height_mm,
        )

    def _confidence(self) -> float:
        position_sigma = math.sqrt(
            (self._covariance[0] + self._covariance[1]) / 2.0
        )
        yaw_sigma = math.sqrt(self._covariance[2])
        cell_scale = min(
            self.config.cell_width_mm,
            self.config.cell_height_mm,
        )
        confidence = (
            1.0
            - min(0.65, position_sigma / max(1.0, cell_scale * 1.5))
            - min(0.25, yaw_sigma / 180.0)
        )
        if not self._continuous_heading_valid:
            confidence *= 0.72
        if "wall_outlier" in self._quality_flags:
            confidence *= 0.85
        return max(0.0, min(1.0, confidence))

    @staticmethod
    def _scalar_update(
        current: float,
        variance: float,
        measurement: float,
        measurement_variance: float,
    ) -> tuple[float, float]:
        gain = variance / (variance + measurement_variance)
        return (
            current + gain * (measurement - current),
            max(0.001, (1.0 - gain) * variance),
        )

    @staticmethod
    def _angle_update(
        current: float,
        variance: float,
        measurement: float,
        measurement_variance: float,
    ) -> tuple[float, float]:
        gain = variance / (variance + measurement_variance)
        updated = normalize_yaw(
            current + gain * angle_delta_deg(measurement, current)
        )
        return updated, max(0.001, (1.0 - gain) * variance)

    @staticmethod
    def _delta_seconds(
        previous: PoseObservation | None,
        current: PoseObservation,
    ) -> float:
        if previous is None:
            return 0.0
        delta_ms = int(current.timestamp_ms) - int(previous.timestamp_ms)
        return max(0.0, min(10.0, delta_ms / 1000.0))
