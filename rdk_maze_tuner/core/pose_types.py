"""Typed contracts for fused pose, wall evidence, and truth evaluation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping


HEADINGS = ("N", "E", "S", "W")
HEADING_YAW_DEG = {
    "N": 0.0,
    "E": 90.0,
    "S": 180.0,
    "W": 270.0,
}


def normalize_yaw(value: float) -> float:
    return float(value) % 360.0


def angle_delta_deg(target: float, current: float) -> float:
    return (float(target) - float(current) + 180.0) % 360.0 - 180.0


@dataclass(frozen=True)
class PoseFusionConfig:
    cell_width_mm: float
    cell_height_mm: float
    cell_ticks: float
    turn_90_ticks: float
    wheel_base_mm: float
    encoder_calibration_distance_mm: float | None = None
    y_axis_down: bool = True
    wall_outlier_limit_mm: float = 180.0

    def __post_init__(self) -> None:
        for name in (
            "cell_width_mm",
            "cell_height_mm",
            "cell_ticks",
            "turn_90_ticks",
            "wheel_base_mm",
            "wall_outlier_limit_mm",
        ):
            if float(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")
        if (
            self.encoder_calibration_distance_mm is not None
            and float(self.encoder_calibration_distance_mm) <= 0
        ):
            raise ValueError(
                "encoder_calibration_distance_mm must be positive"
            )

    @property
    def mm_per_tick(self) -> float:
        calibration_mm = self.encoder_calibration_distance_mm
        if calibration_mm is None:
            calibration_mm = (
                float(self.cell_width_mm) + float(self.cell_height_mm)
            ) / 2.0
        return float(calibration_mm) / float(self.cell_ticks)

    @property
    def yaw_deg_per_differential_tick(self) -> float:
        return 90.0 / (2.0 * float(self.turn_90_ticks))


@dataclass(frozen=True)
class PoseObservation:
    timestamp_ms: int
    enc_left: int
    enc_right: int
    imu_available: bool = False
    imu_yaw_deg: float | None = None
    yaw_rate_dps: float | None = None
    accel_forward_mps2: float | None = None
    quality_flags: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PoseObservation":
        timestamp = value.get("ts_ms", value.get("uptime_ms", 0))
        return cls(
            timestamp_ms=int(timestamp or 0),
            enc_left=int(value.get("enc_left") or 0),
            enc_right=int(value.get("enc_right") or 0),
            imu_available=bool(value.get("imu_available", False)),
            imu_yaw_deg=_optional_float(
                value.get("imu_yaw_deg", value.get("yaw_deg"))
            ),
            yaw_rate_dps=_optional_float(value.get("yaw_rate_dps")),
            accel_forward_mps2=_optional_float(
                value.get("accel_forward_mps2")
            ),
            quality_flags=tuple(
                str(item)
                for item in value.get("quality_flags", ())
                if str(item)
            ),
        )


@dataclass(frozen=True)
class WallConstraint:
    direction: str
    wall_coordinate_mm: float
    distance_mm: float
    variance_mm2: float = 400.0
    max_residual_mm: float | None = None

    def __post_init__(self) -> None:
        if self.direction not in HEADINGS:
            raise ValueError("wall direction must be N, E, S, or W")
        if float(self.distance_mm) < 0:
            raise ValueError("wall distance must not be negative")
        if float(self.variance_mm2) <= 0:
            raise ValueError("wall variance must be positive")

    def position_measurement(self) -> tuple[str, float]:
        coordinate = float(self.wall_coordinate_mm)
        distance = float(self.distance_mm)
        if self.direction == "N":
            return "y", coordinate + distance
        if self.direction == "S":
            return "y", coordinate - distance
        if self.direction == "E":
            return "x", coordinate - distance
        return "x", coordinate + distance


@dataclass(frozen=True)
class PoseEstimate:
    grid_cell: tuple[int, int]
    heading: str
    x_mm: float
    y_mm: float
    yaw_deg: float
    speed_mm_s: float
    angular_velocity_dps: float
    covariance: tuple[float, float, float]
    confidence: float
    correction_source: str
    wall_residual_mm: float | None
    continuous_heading_valid: bool
    quality_flags: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "grid_cell": [self.grid_cell[0], self.grid_cell[1]],
            "heading": self.heading,
            "x_mm": self.x_mm,
            "y_mm": self.y_mm,
            "yaw_deg": self.yaw_deg,
            "speed_mm_s": self.speed_mm_s,
            "angular_velocity_dps": self.angular_velocity_dps,
            "covariance": list(self.covariance),
            "confidence": self.confidence,
            "correction_source": self.correction_source,
            "wall_residual_mm": self.wall_residual_mm,
            "continuous_heading_valid": self.continuous_heading_valid,
            "quality_flags": list(self.quality_flags),
        }


@dataclass(frozen=True)
class TruthPose:
    x_mm: float
    y_mm: float
    yaw_deg: float

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "TruthPose":
        if not isinstance(value, Mapping):
            raise ValueError("simulation truth must be an object")
        return cls(
            x_mm=float(value["x_mm"]),
            y_mm=float(value["y_mm"]),
            yaw_deg=normalize_yaw(float(value["yaw_deg"])),
        )


@dataclass(frozen=True)
class PoseEvaluation:
    position_error_mm: float
    yaw_error_deg: float

    def to_dict(self) -> dict[str, float]:
        return {
            "position_error_mm": self.position_error_mm,
            "yaw_error_deg": self.yaw_error_deg,
        }


def evaluate_pose(
    estimate: PoseEstimate,
    truth: TruthPose,
) -> PoseEvaluation:
    """Compare against truth without feeding truth back into the estimator."""
    return PoseEvaluation(
        position_error_mm=round(
            math.hypot(
                estimate.x_mm - truth.x_mm,
                estimate.y_mm - truth.y_mm,
            ),
            3,
        ),
        yaw_error_deg=round(
            abs(angle_delta_deg(truth.yaw_deg, estimate.yaw_deg)),
            3,
        ),
    )


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
