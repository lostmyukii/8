"""Supervisor-only ground truth for evaluation, never control feedback."""

from __future__ import annotations

import math
from typing import Any

from .physical_types import PhysicalDeviceError


SIMULATION_TRUTH_FIELDS = frozenset(
    {
        "x_mm",
        "y_mm",
        "yaw_deg",
        "linear_speed_mm_s",
        "angular_velocity_dps",
        "left_slip_rate",
        "right_slip_rate",
        "active_surface",
        "collision_count",
    }
)


class TruthObserver:
    def __init__(self, supervisor_node: Any) -> None:
        self._node = supervisor_node

    def observe(
        self,
        *,
        wheel_linear_left_mps: float,
        wheel_linear_right_mps: float,
        active_surface: str,
        collision_count: int,
    ) -> dict[str, Any]:
        try:
            position = tuple(
                float(value) for value in self._node.getPosition()
            )
            orientation = tuple(
                float(value) for value in self._node.getOrientation()
            )
            velocity = tuple(
                float(value) for value in self._node.getVelocity()
            )
        except Exception as exc:
            raise PhysicalDeviceError(
                "SIM_PHYSICS_ERROR",
                f"cannot observe Supervisor truth: {exc}",
            ) from exc
        if (
            len(position) != 3
            or len(orientation) != 9
            or len(velocity) != 6
            or not all(
                math.isfinite(value)
                for value in (*position, *orientation, *velocity)
            )
        ):
            raise PhysicalDeviceError(
                "SIM_PHYSICS_ERROR",
                "Supervisor truth contains non-finite values",
            )

        # Local forward is -Z in the explicit Y-up (NUE) physical worlds.
        forward_x = -orientation[2]
        forward_z = -orientation[8]
        yaw_deg = math.degrees(
            math.atan2(forward_x, -forward_z)
        ) % 360.0
        linear_speed_mps = math.hypot(velocity[0], velocity[2])
        return {
            "x_mm": round(position[0] * 1000.0, 6),
            "y_mm": round(position[2] * 1000.0, 6),
            "yaw_deg": round(yaw_deg, 6),
            "linear_speed_mm_s": round(
                linear_speed_mps * 1000.0,
                6,
            ),
            "angular_velocity_dps": round(
                math.degrees(velocity[4]),
                6,
            ),
            "left_slip_rate": _truth_slip(
                wheel_linear_left_mps,
                linear_speed_mps,
            ),
            "right_slip_rate": _truth_slip(
                wheel_linear_right_mps,
                linear_speed_mps,
            ),
            "active_surface": str(active_surface),
            "collision_count": max(0, int(collision_count)),
        }


def _truth_slip(
    wheel_linear_mps: float,
    body_linear_mps: float,
) -> float:
    wheel_speed = abs(float(wheel_linear_mps))
    if wheel_speed < 0.001:
        return 0.0 if body_linear_mps < 0.001 else 1.0
    return round(
        max(
            0.0,
            min(1.0, 1.0 - abs(float(body_linear_mps)) / wheel_speed),
        ),
        6,
    )
