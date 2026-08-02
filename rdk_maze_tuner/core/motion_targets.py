"""Resolve recorded motion targets from map geometry and calibration."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .maze_map import Direction, MazeMap, PlannedAction
from .motion_evidence import RecoverySuggestion


MAX_TARGET_TICKS = 2_147_483_647


class MotionTargetError(ValueError):
    """Raised when a motion target cannot be represented safely."""


@dataclass(frozen=True)
class MotionTarget:
    action_name: str
    direction: str | None
    distance_mm: float | None
    ticks_per_mm: float
    target_ticks: int
    source: str
    target_angle_deg: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "action_name": self.action_name,
            "direction": self.direction,
            "distance_mm": self.distance_mm,
            "ticks_per_mm": self.ticks_per_mm,
            "target_ticks": self.target_ticks,
            "target_source": self.source,
            "target_angle_deg": self.target_angle_deg,
        }


class MotionTargetResolver:
    """Pure conversion from planned action and map scale to encoder ticks."""

    def __init__(
        self,
        *,
        ticks_per_mm: float,
        fallback_cell_size_mm: float,
        turn_90_ticks: int,
        turn_180_ticks: int,
        max_target_ticks: int = MAX_TARGET_TICKS,
    ) -> None:
        self.ticks_per_mm = _positive_number(
            ticks_per_mm,
            "ticks_per_mm",
        )
        self.fallback_cell_size_mm = _positive_number(
            fallback_cell_size_mm,
            "fallback_cell_size_mm",
        )
        self.turn_90_ticks = _positive_integer(
            turn_90_ticks,
            "turn_90_ticks",
        )
        self.turn_180_ticks = _positive_integer(
            turn_180_ticks,
            "turn_180_ticks",
        )
        self.max_target_ticks = _positive_integer(
            max_target_ticks,
            "max_target_ticks",
        )

    @classmethod
    def from_params(cls, params) -> "MotionTargetResolver":
        fallback_mm = (
            float(params.get("robot.cell_size_cm")) * 10.0
        )
        static_cell_ticks = int(params.get("motion.cell_ticks"))
        return cls(
            ticks_per_mm=static_cell_ticks / fallback_mm,
            fallback_cell_size_mm=fallback_mm,
            turn_90_ticks=int(
                params.get("motion.turn_90_ticks")
            ),
            turn_180_ticks=int(
                params.get("motion.turn_180_ticks")
            ),
        )

    def resolve(
        self,
        action: PlannedAction,
        maze: MazeMap,
    ) -> MotionTarget:
        if action.name == "move_cell":
            return self._move_target(action, maze)
        if action.name in {"turn_left", "turn_right"}:
            return MotionTarget(
                action_name=action.name,
                direction=None,
                distance_mm=None,
                ticks_per_mm=self.ticks_per_mm,
                target_ticks=self._checked_ticks(
                    self.turn_90_ticks
                ),
                source="motion.turn_calibration",
                target_angle_deg=(
                    -90.0 if action.name == "turn_left" else 90.0
                ),
            )
        if action.name == "turn_back":
            return MotionTarget(
                action_name=action.name,
                direction=None,
                distance_mm=None,
                ticks_per_mm=self.ticks_per_mm,
                target_ticks=self._checked_ticks(
                    self.turn_180_ticks
                ),
                source="motion.turn_calibration",
                target_angle_deg=180.0,
            )
        raise MotionTargetError(
            f"unsupported motion action: {action.name}"
        )

    def resolve_recovery(
        self,
        recovery: RecoverySuggestion,
        maze: MazeMap,
    ) -> MotionTarget:
        if recovery.kind == "nudge_forward":
            return self._nudge_target(recovery, maze)
        if recovery.kind == "align_heading":
            return self._alignment_target(recovery)
        raise MotionTargetError(
            f"unsupported recovery action: {recovery.kind}"
        )

    @staticmethod
    def recovery_speed(
        target: MotionTarget,
        *,
        base_speed: float,
        turn_speed: float,
    ) -> float:
        base = _positive_number(base_speed, "base_speed")
        turn = _positive_number(turn_speed, "turn_speed")
        if target.action_name == "nudge_forward":
            return min(0.10, base * 0.5)
        if target.action_name == "align_heading":
            return min(0.09, turn * 0.5)
        raise MotionTargetError(
            f"unsupported recovery target: {target.action_name}"
        )

    def _move_target(
        self,
        action: PlannedAction,
        maze: MazeMap,
    ) -> MotionTarget:
        direction = action.direction or maze.heading
        try:
            direction = Direction(direction)
        except ValueError as exc:
            raise MotionTargetError(
                f"unsupported movement direction: {direction}"
            ) from exc
        if direction in {Direction.NORTH, Direction.SOUTH}:
            raw_distance = maze.cell_height_mm
            source = "map.cell_height_mm"
        else:
            raw_distance = maze.cell_width_mm
            source = "map.cell_width_mm"
        if raw_distance is None:
            raw_distance = self.fallback_cell_size_mm
            source = "robot.cell_size_cm"
        distance = _positive_number(raw_distance, "distance_mm")
        ticks = self._checked_ticks(
            int(round(distance * self.ticks_per_mm))
        )
        return MotionTarget(
            action_name=action.name,
            direction=direction.value,
            distance_mm=distance,
            ticks_per_mm=self.ticks_per_mm,
            target_ticks=ticks,
            source=source,
        )

    def _nudge_target(
        self,
        recovery: RecoverySuggestion,
        maze: MazeMap,
    ) -> MotionTarget:
        direction = maze.heading
        raw_cell_distance = (
            maze.cell_height_mm
            if direction in {Direction.NORTH, Direction.SOUTH}
            else maze.cell_width_mm
        )
        cell_distance = _positive_number(
            raw_cell_distance or self.fallback_cell_size_mm,
            "cell_distance_mm",
        )
        remaining = _positive_number(
            recovery.remaining_distance_mm,
            "remaining_distance_mm",
        )
        caps = [remaining, cell_distance * 0.25]
        if recovery.max_distance_mm is not None:
            caps.append(
                _positive_number(
                    recovery.max_distance_mm,
                    "max_distance_mm",
                )
            )
        distance = min(caps)
        return MotionTarget(
            action_name="nudge_forward",
            direction=direction.value,
            distance_mm=distance,
            ticks_per_mm=self.ticks_per_mm,
            target_ticks=self._checked_ticks(
                int(round(distance * self.ticks_per_mm))
            ),
            source="recovery.nudge_forward",
        )

    def _alignment_target(
        self,
        recovery: RecoverySuggestion,
    ) -> MotionTarget:
        delta = float(recovery.heading_delta_deg or 0.0)
        if not math.isfinite(delta) or delta == 0:
            raise MotionTargetError(
                "heading_delta_deg must be finite and non-zero"
            )
        max_heading = min(
            15.0,
            _positive_number(
                recovery.max_heading_deg or 15.0,
                "max_heading_deg",
            ),
        )
        angle = math.copysign(min(abs(delta), max_heading), delta)
        ticks = self._checked_ticks(
            max(1, int(round(self.turn_90_ticks * abs(angle) / 90.0)))
        )
        return MotionTarget(
            action_name="align_heading",
            direction="left" if angle < 0 else "right",
            distance_mm=None,
            ticks_per_mm=self.ticks_per_mm,
            target_ticks=ticks,
            source="recovery.align_heading",
            target_angle_deg=angle,
        )

    def _checked_ticks(self, value: int) -> int:
        ticks = _positive_integer(value, "target_ticks")
        if ticks > self.max_target_ticks:
            raise MotionTargetError(
                "target_ticks exceeds signed 32-bit safe range"
            )
        return ticks


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise MotionTargetError(f"{name} must be a positive number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MotionTargetError(
            f"{name} must be a positive number"
        ) from exc
    if not math.isfinite(number) or number <= 0:
        raise MotionTargetError(f"{name} must be a positive number")
    return number


def _positive_integer(value: object, name: str) -> int:
    number = _positive_number(value, name)
    integer = int(number)
    if number != integer:
        raise MotionTargetError(f"{name} must be an integer")
    return integer
