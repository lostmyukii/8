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
        "body_longitudinal_speed_mm_s",
        "angular_velocity_dps",
        "left_slip_rate",
        "right_slip_rate",
        "active_surface",
        "collision_count",
    }
)
_LOCAL_PATCH_X_BOUNDS_M = (0.25, 0.75)
_LOCAL_PATCH_Z_BOUNDS_M = (-0.875, -0.375)


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
        body_longitudinal_mps = (
            velocity[0] * forward_x
            + velocity[2] * forward_z
        )
        return {
            "x_mm": round(position[0] * 1000.0, 6),
            "y_mm": round(position[2] * 1000.0, 6),
            "yaw_deg": round(yaw_deg, 6),
            "linear_speed_mm_s": round(
                linear_speed_mps * 1000.0,
                6,
            ),
            "body_longitudinal_speed_mm_s": round(
                body_longitudinal_mps * 1000.0,
                6,
            ),
            "angular_velocity_dps": round(
                math.degrees(velocity[4]),
                6,
            ),
            "left_slip_rate": compute_truth_slip_rate(
                wheel_linear_left_mps,
                body_longitudinal_mps,
            ),
            "right_slip_rate": compute_truth_slip_rate(
                wheel_linear_right_mps,
                body_longitudinal_mps,
            ),
            "active_surface": _observed_surface(
                requested=str(active_surface),
                x_m=position[0],
                z_m=position[2],
            ),
            "collision_count": max(0, int(collision_count)),
        }


def compute_truth_slip_rate(
    wheel_linear_mps: float,
    body_longitudinal_mps: float,
    *,
    epsilon: float = 0.001,
) -> float:
    values = (
        float(wheel_linear_mps),
        float(body_longitudinal_mps),
        float(epsilon),
    )
    if not all(math.isfinite(value) for value in values):
        raise PhysicalDeviceError(
            "SIM_PHYSICS_ERROR",
            "truth slip inputs must be finite",
        )
    if epsilon <= 0:
        raise ValueError("truth slip epsilon must be positive")
    return round(
        (values[0] - values[1])
        / max(abs(values[0]), values[2]),
        6,
    )


def _observed_surface(
    *,
    requested: str,
    x_m: float,
    z_m: float,
) -> str:
    if requested != "local_patch":
        return requested
    inside = (
        _LOCAL_PATCH_X_BOUNDS_M[0]
        <= float(x_m)
        <= _LOCAL_PATCH_X_BOUNDS_M[1]
        and _LOCAL_PATCH_Z_BOUNDS_M[0]
        <= float(z_m)
        <= _LOCAL_PATCH_Z_BOUNDS_M[1]
    )
    return "local_patch" if inside else "normal"
