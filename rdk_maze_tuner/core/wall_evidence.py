"""Shared immutable-map wall constraints and longitudinal motion evidence."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Mapping

from .maze_map import Direction, MazeMap, ORDER
from .pose_types import WallConstraint


@dataclass(frozen=True)
class WallDistanceReference:
    local_sensor: str
    direction: Direction
    wall_coordinate_mm: float
    distance_mm: float

    @property
    def key(self) -> tuple[str, float]:
        return self.direction.value, self.wall_coordinate_mm


@dataclass(frozen=True)
class WallEvidenceSnapshot:
    constraints: tuple[WallConstraint, ...]
    references: tuple[WallDistanceReference, ...]

    @property
    def independent_axes(self) -> int:
        return len(
            {
                constraint.position_measurement()[0]
                for constraint in self.constraints
            }
        )

    def reference_for(
        self,
        local_sensor: str,
    ) -> WallDistanceReference | None:
        return next(
            (
                reference
                for reference in self.references
                if reference.local_sensor == local_sensor
            ),
            None,
        )


class WallEvidenceBuilder:
    """Build pose constraints without changing map or estimator state."""

    _SENSORS = (
        ("front", "fusion_front_mm", "front_mm"),
        ("left", "fusion_left_mm", "left_mm"),
        ("right", "fusion_right_mm", "right_mm"),
        ("back", "fusion_back_mm", "back_mm"),
    )

    def __init__(
        self,
        *,
        maze: MazeMap,
        fallback_cell_width_mm: float,
        fallback_cell_height_mm: float,
        variance_mm2: float = 625.0,
    ) -> None:
        self.maze = maze
        self.cell_width_mm = float(
            maze.cell_width_mm or fallback_cell_width_mm
        )
        self.cell_height_mm = float(
            maze.cell_height_mm or fallback_cell_height_mm
        )
        self.variance_mm2 = float(variance_mm2)

    def build(
        self,
        telemetry: Mapping[str, Any],
        *,
        cell: tuple[int, int],
        heading: Direction | str,
    ) -> WallEvidenceSnapshot:
        if not getattr(self.maze, "_screen_coordinates", False):
            return WallEvidenceSnapshot((), ())
        normalized_heading = Direction(heading)
        constraints: list[WallConstraint] = []
        references: list[WallDistanceReference] = []
        for local, fusion_field, raw_field in self._SENSORS:
            distance = _valid_distance(
                telemetry.get(
                    fusion_field,
                    telemetry.get(raw_field),
                )
            )
            if distance is None:
                continue
            direction = local_to_global(normalized_heading, local)
            coordinate = self.nearest_planned_wall_coordinate(
                cell=cell,
                direction=direction,
            )
            if coordinate is None:
                continue
            constraints.append(
                WallConstraint(
                    direction=direction.value,
                    wall_coordinate_mm=coordinate,
                    distance_mm=distance,
                    variance_mm2=self.variance_mm2,
                )
            )
            references.append(
                WallDistanceReference(
                    local_sensor=local,
                    direction=direction,
                    wall_coordinate_mm=coordinate,
                    distance_mm=distance,
                )
            )
        return WallEvidenceSnapshot(
            constraints=tuple(constraints),
            references=tuple(references),
        )

    def nearest_planned_wall_coordinate(
        self,
        *,
        cell: tuple[int, int],
        direction: Direction,
    ) -> float | None:
        rows = self.maze.rows
        cols = self.maze.cols
        if rows is None or cols is None:
            return None
        current = cell
        for _ in range(rows * cols + 1):
            x, y = current
            if not 0 <= x < cols or not 0 <= y < rows:
                return None
            planned_wall = self.maze.cell(current).planned_walls[
                direction.value
            ]
            if planned_wall is True:
                return {
                    "N": y * self.cell_height_mm,
                    "S": (y + 1) * self.cell_height_mm,
                    "W": x * self.cell_width_mm,
                    "E": (x + 1) * self.cell_width_mm,
                }[direction.value]
            if planned_wall is not False:
                return None
            current = self.maze.neighbor(current, direction)
        return None

    @staticmethod
    def longitudinal_displacement(
        before: WallEvidenceSnapshot,
        after: WallEvidenceSnapshot,
    ) -> float | None:
        candidates: list[float] = []
        for local_sensor, sign in (("front", 1.0), ("back", -1.0)):
            first = before.reference_for(local_sensor)
            second = after.reference_for(local_sensor)
            if first is None or second is None or first.key != second.key:
                continue
            candidates.append(
                sign * (first.distance_mm - second.distance_mm)
            )
        if not candidates:
            return None
        forward = sum(candidates) / len(candidates)
        return max(0.0, forward)


def local_to_global(
    heading: Direction | str,
    local_direction: str,
) -> Direction:
    offset = {"front": 0, "right": 1, "back": 2, "left": 3}[
        local_direction
    ]
    normalized = Direction(heading)
    return ORDER[(ORDER.index(normalized) + offset) % 4]


def _valid_distance(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        distance = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(distance) or not 0.0 <= distance <= 5000.0:
        return None
    return distance
