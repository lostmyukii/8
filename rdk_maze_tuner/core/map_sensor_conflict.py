"""Pure evidence gate for immutable-map and live-ToF conflicts."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from numbers import Real

from .maze_map import Coord, Direction


MAP_SENSOR_CONFLICT = "MAP_SENSOR_CONFLICT"


@dataclass(frozen=True)
class MapSensorConflict:
    code: str
    run_id: str | None
    cell: Coord
    direction: Direction
    sample_count: int
    distance_mm: float
    wall_threshold_mm: float

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "run_id": self.run_id,
            "cell": [self.cell[0], self.cell[1]],
            "direction": self.direction.value,
            "sample_count": self.sample_count,
            "distance_mm": self.distance_mm,
            "wall_threshold_mm": self.wall_threshold_mm,
        }


class MapSensorConflictDetector:
    """Latch a conflict after repeated valid wall evidence in a planned opening."""

    def __init__(
        self,
        *,
        required_consecutive_samples: int = 3,
        max_valid_distance_mm: float = 8190,
    ) -> None:
        if (
            not isinstance(required_consecutive_samples, int)
            or isinstance(required_consecutive_samples, bool)
            or required_consecutive_samples < 1
        ):
            raise ValueError("required_consecutive_samples must be a positive integer")
        if not _valid_positive_number(max_valid_distance_mm):
            raise ValueError("max_valid_distance_mm must be positive and finite")
        self.required_consecutive_samples = required_consecutive_samples
        self.max_valid_distance_mm = float(max_valid_distance_mm)
        self.run_id: str | None = None
        self.latched_conflict: MapSensorConflict | None = None
        self._pending_key: tuple[Coord, Direction] | None = None
        self._pending_count = 0

    def reset(self, *, run_id: str | None = None) -> None:
        """Explicit task-reset boundary; no sample can silently clear a latch."""

        self.run_id = str(run_id) if run_id is not None else None
        self.latched_conflict = None
        self._clear_pending()

    def observe(
        self,
        *,
        cell: Coord,
        direction: Direction | str,
        planned_wall: bool | None,
        distance_mm: float | None,
        wall_threshold_mm: float,
    ) -> MapSensorConflict | None:
        if self.latched_conflict is not None:
            return self.latched_conflict

        normalized_direction = Direction(direction)
        if planned_wall is not False:
            self._clear_pending()
            return None
        if not _valid_positive_number(distance_mm):
            self._clear_pending()
            return None
        if float(distance_mm) > self.max_valid_distance_mm:
            self._clear_pending()
            return None
        if not _valid_positive_number(wall_threshold_mm):
            raise ValueError("wall_threshold_mm must be positive and finite")
        if float(distance_mm) >= float(wall_threshold_mm):
            self._clear_pending()
            return None

        key = (cell, normalized_direction)
        if key == self._pending_key:
            self._pending_count += 1
        else:
            self._pending_key = key
            self._pending_count = 1
        if self._pending_count < self.required_consecutive_samples:
            return None

        self.latched_conflict = MapSensorConflict(
            code=MAP_SENSOR_CONFLICT,
            run_id=self.run_id,
            cell=cell,
            direction=normalized_direction,
            sample_count=self._pending_count,
            distance_mm=float(distance_mm),
            wall_threshold_mm=float(wall_threshold_mm),
        )
        return self.latched_conflict

    def _clear_pending(self) -> None:
        self._pending_key = None
        self._pending_count = 0


def _valid_positive_number(value: object) -> bool:
    return (
        isinstance(value, Real)
        and not isinstance(value, bool)
        and isfinite(float(value))
        and float(value) > 0
    )
