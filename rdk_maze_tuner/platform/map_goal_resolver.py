"""Resolve immutable map-owned goals for automatic maze tasks."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rdk_maze_tuner.core.maze_definition import MapDefinition

from .map_repository import MapVersion


class MapGoalResolutionError(ValueError):
    """Raised when a map version cannot produce a trustworthy goal."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ResolvedMapGoal:
    """Immutable goal decision together with its source evidence."""

    cell: tuple[int, int]
    candidate_cells: tuple[tuple[int, int], ...]
    source_map_version: str
    source_map_digest: str
    resolution: str
    path_length_cells: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "map_goal",
            "cell": list(self.cell),
            "candidate_cells": [list(cell) for cell in self.candidate_cells],
            "source_map_version": self.source_map_version,
            "source_map_digest": self.source_map_digest,
            "resolution": self.resolution,
            "path_length_cells": self.path_length_cells,
        }


class MapGoalResolver:
    """Select the deterministic reachable goal from one immutable map version."""

    def __init__(self, *, map_provider: Callable[[str], MapVersion]) -> None:
        self._map_provider = map_provider

    def resolve(self, map_version_id: str) -> ResolvedMapGoal:
        requested_version = str(map_version_id).strip()
        version = self._map_provider(requested_version)

        if version.version_id != requested_version:
            raise MapGoalResolutionError(
                "MAP_VERSION_MISMATCH",
                "map provider returned a different immutable version",
            )

        actual_digest = version.definition.content_digest
        if version.digest != actual_digest:
            raise MapGoalResolutionError(
                "MAP_DIGEST_MISMATCH",
                "stored map digest does not match the map definition",
            )

        candidates = tuple(
            sorted(version.definition.goals, key=lambda cell: (cell[1], cell[0]))
        )
        if not candidates:
            raise MapGoalResolutionError(
                "MAP_GOAL_MISSING",
                "map version does not define an automatic-task goal",
            )

        distances = _reachable_distances(version.definition)
        reachable = [
            (distances[cell], cell[1], cell[0], cell)
            for cell in candidates
            if cell in distances
        ]
        if not reachable:
            raise MapGoalResolutionError(
                "MAP_GOAL_UNREACHABLE",
                "none of the map-owned goals is reachable from the start cell",
            )

        distance, _y, _x, selected = min(reachable)
        return ResolvedMapGoal(
            cell=selected,
            candidate_cells=candidates,
            source_map_version=version.version_id,
            source_map_digest=version.digest,
            resolution="single" if len(candidates) == 1 else "shortest_path",
            path_length_cells=distance,
        )


def _reachable_distances(definition: MapDefinition) -> dict[tuple[int, int], int]:
    start = (definition.start.x, definition.start.y)
    distances = {start: 0}
    pending = deque([start])
    directions = (
        ("N", 0, -1),
        ("E", 1, 0),
        ("S", 0, 1),
        ("W", -1, 0),
    )

    while pending:
        cell = pending.popleft()
        blocked = definition.blocked_directions(cell)
        for name, dx, dy in directions:
            if name in blocked:
                continue
            neighbor = (cell[0] + dx, cell[1] + dy)
            if not (0 <= neighbor[0] < definition.cols):
                continue
            if not (0 <= neighbor[1] < definition.rows):
                continue
            if neighbor in distances:
                continue
            distances[neighbor] = distances[cell] + 1
            pending.append(neighbor)

    return distances
