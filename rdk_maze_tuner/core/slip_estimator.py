"""Estimated wheel-slip evidence from encoders, IMU, and wall motion."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .pose_types import angle_delta_deg


@dataclass(frozen=True)
class SlipEstimatorConfig:
    mm_per_tick: float
    wheel_base_mm: float
    minimum_wheel_motion_mm: float = 5.0

    def __post_init__(self) -> None:
        if self.mm_per_tick <= 0:
            raise ValueError("mm_per_tick must be positive")
        if self.wheel_base_mm <= 0:
            raise ValueError("wheel_base_mm must be positive")
        if self.minimum_wheel_motion_mm <= 0:
            raise ValueError("minimum_wheel_motion_mm must be positive")


@dataclass(frozen=True)
class SlipEstimate:
    left_slip_rate: float | None
    right_slip_rate: float | None
    overall_slip_rate: float | None
    equivalent_friction: float | None
    friction_profile: str
    quality: str
    confidence: float
    quality_flags: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "left_slip_rate": self.left_slip_rate,
            "right_slip_rate": self.right_slip_rate,
            "overall_slip_rate": self.overall_slip_rate,
            "equivalent_friction": self.equivalent_friction,
            "friction_profile": self.friction_profile,
            "quality": self.quality,
            "confidence": self.confidence,
            "quality_flags": list(self.quality_flags),
            "is_physical_truth": False,
        }


class SlipEstimator:
    def __init__(self, config: SlipEstimatorConfig) -> None:
        self.config = config
        self._last_timestamp_ms: int | None = None
        self._last_enc_left: int | None = None
        self._last_enc_right: int | None = None
        self._last_imu_yaw_deg: float | None = None
        self._estimate = self._insufficient()

    def update(
        self,
        *,
        timestamp_ms: int,
        enc_left: int,
        enc_right: int,
        imu_available: bool,
        imu_yaw_deg: float | None,
        external_distance_mm: float | None,
    ) -> SlipEstimate:
        if self._last_enc_left is None or self._last_enc_right is None:
            self._remember(
                timestamp_ms,
                enc_left,
                enc_right,
                imu_yaw_deg,
            )
            self._estimate = self._insufficient("baseline_only")
            return self._estimate

        predicted_left = (
            int(enc_left) - self._last_enc_left
        ) * self.config.mm_per_tick
        predicted_right = (
            int(enc_right) - self._last_enc_right
        ) * self.config.mm_per_tick
        yaw_delta = None
        if (
            imu_available
            and imu_yaw_deg is not None
            and self._last_imu_yaw_deg is not None
        ):
            yaw_delta = angle_delta_deg(
                imu_yaw_deg,
                self._last_imu_yaw_deg,
            )

        external_center = (
            float(external_distance_mm)
            if external_distance_mm is not None
            else None
        )
        if (
            external_center is None
            and yaw_delta is not None
            and abs(predicted_left + predicted_right)
            <= self.config.minimum_wheel_motion_mm * 2.0
        ):
            external_center = 0.0

        if external_center is None:
            result = self._insufficient()
        else:
            observed_yaw_rad = math.radians(yaw_delta or 0.0)
            observed_left = (
                external_center
                + self.config.wheel_base_mm * observed_yaw_rad / 2.0
            )
            observed_right = (
                external_center
                - self.config.wheel_base_mm * observed_yaw_rad / 2.0
            )
            left_slip = self._slip_rate(
                predicted_left,
                observed_left,
            )
            right_slip = self._slip_rate(
                predicted_right,
                observed_right,
            )
            if left_slip is None or right_slip is None:
                result = self._insufficient("motion_too_small")
            else:
                overall = max(left_slip, right_slip)
                flags = {"estimated_not_physical_truth"}
                if overall >= 0.35:
                    flags.add("wheelspin_suspected")
                if abs(left_slip - right_slip) >= 0.15:
                    flags.add("asymmetric_slip")
                profile = (
                    "normal"
                    if overall < 0.08
                    else "reduced"
                    if overall < 0.30
                    else "low"
                )
                result = SlipEstimate(
                    left_slip_rate=round(left_slip, 4),
                    right_slip_rate=round(right_slip, 4),
                    overall_slip_rate=round(overall, 4),
                    equivalent_friction=round(
                        max(0.1, 1.0 - overall * 0.9),
                        4,
                    ),
                    friction_profile=profile,
                    quality="good" if imu_available else "degraded",
                    confidence=0.9 if imu_available else 0.6,
                    quality_flags=tuple(sorted(flags)),
                )

        self._remember(
            timestamp_ms,
            enc_left,
            enc_right,
            imu_yaw_deg,
        )
        self._estimate = result
        return result

    def estimate(self) -> SlipEstimate:
        return self._estimate

    def _remember(
        self,
        timestamp_ms: int,
        enc_left: int,
        enc_right: int,
        imu_yaw_deg: float | None,
    ) -> None:
        self._last_timestamp_ms = int(timestamp_ms)
        self._last_enc_left = int(enc_left)
        self._last_enc_right = int(enc_right)
        self._last_imu_yaw_deg = (
            None if imu_yaw_deg is None else float(imu_yaw_deg)
        )

    def _slip_rate(
        self,
        predicted_mm: float,
        observed_mm: float,
    ) -> float | None:
        minimum = self.config.minimum_wheel_motion_mm
        if abs(predicted_mm) < minimum:
            return 0.0 if abs(observed_mm) < minimum else None
        if predicted_mm * observed_mm < 0:
            return 1.0
        ratio = abs(observed_mm) / abs(predicted_mm)
        return max(0.0, min(1.0, 1.0 - min(1.0, ratio)))

    @staticmethod
    def _insufficient(
        reason: str = "insufficient_external_evidence",
    ) -> SlipEstimate:
        flags = {"insufficient_external_evidence"}
        if reason != "insufficient_external_evidence":
            flags.add(reason)
        return SlipEstimate(
            left_slip_rate=None,
            right_slip_rate=None,
            overall_slip_rate=None,
            equivalent_friction=None,
            friction_profile="unknown",
            quality="insufficient",
            confidence=0.0,
            quality_flags=tuple(sorted(flags)),
        )
