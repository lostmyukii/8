"""Validation and canonicalization for versioned grid-maze definitions."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any

from .maze_definition import MapDefinition, StartPose, WallSegment


HEADINGS = frozenset(("N", "E", "S", "W"))
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MIN_CELL_MM = 100
MAX_CELL_MM = 5_000
MIN_PASSAGE_MM = 120


class MazeValidationError(ValueError):
    """Raised when a drawing cannot become a safe structural maze."""


def validate_map_definition(payload: Mapping[str, Any]) -> MapDefinition:
    if not isinstance(payload, Mapping):
        raise MazeValidationError("map definition must be an object")

    rows = _bounded_int(payload.get("rows"), "rows", 1, 64)
    cols = _bounded_int(payload.get("cols"), "cols", 1, 64)
    cell_width_mm = _bounded_int(
        payload.get("cell_width_mm"),
        "cell_width_mm",
        MIN_CELL_MM,
        MAX_CELL_MM,
    )
    cell_height_mm = _bounded_int(
        payload.get("cell_height_mm"),
        "cell_height_mm",
        MIN_CELL_MM,
        MAX_CELL_MM,
    )
    wall_thickness_mm = _bounded_int(
        payload.get("wall_thickness_mm"),
        "wall_thickness_mm",
        1,
        min(cell_width_mm, cell_height_mm) - 1,
    )
    wall_height_mm = _bounded_int(
        payload.get("wall_height_mm"),
        "wall_height_mm",
        1,
        2_000,
    )
    if cell_width_mm - wall_thickness_mm < MIN_PASSAGE_MM:
        raise MazeValidationError(
            "cell_width_mm leaves insufficient passage width"
        )
    if cell_height_mm - wall_thickness_mm < MIN_PASSAGE_MM:
        raise MazeValidationError(
            "cell_height_mm leaves insufficient passage width"
        )

    start = _parse_start(payload.get("start"), rows=rows, cols=cols)
    goals = _parse_goals(payload.get("goals"), rows=rows, cols=cols)
    walls = _parse_walls(payload.get("walls"), rows=rows, cols=cols)
    source_image_digest = _parse_source_digest(
        payload.get("source_image_digest")
    )
    wall_keys = {_wall_key(wall) for wall in walls}
    _require_closed_boundary(wall_keys, rows=rows, cols=cols)
    _require_reachable_goal(
        wall_keys,
        rows=rows,
        cols=cols,
        start=(start.x, start.y),
        goals=set(goals),
    )
    return MapDefinition(
        rows=rows,
        cols=cols,
        cell_width_mm=cell_width_mm,
        cell_height_mm=cell_height_mm,
        wall_thickness_mm=wall_thickness_mm,
        wall_height_mm=wall_height_mm,
        start=start,
        goals=tuple(sorted(goals, key=lambda item: (item[1], item[0]))),
        walls=tuple(sorted(walls, key=_wall_sort_key)),
        source_image_digest=source_image_digest,
    )


def _bounded_int(
    value: Any,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise MazeValidationError(
            f"{name} must be an integer between {minimum} and {maximum}"
        )
    return value


def _parse_start(value: Any, *, rows: int, cols: int) -> StartPose:
    if not isinstance(value, Mapping):
        raise MazeValidationError("start must be an object")
    x = _bounded_int(value.get("x"), "start.x", 0, cols - 1)
    y = _bounded_int(value.get("y"), "start.y", 0, rows - 1)
    heading = str(value.get("heading") or "")
    if heading not in HEADINGS:
        raise MazeValidationError("start.heading must be N, E, S or W")
    return StartPose(x=x, y=y, heading=heading)


def _parse_goals(
    value: Any,
    *,
    rows: int,
    cols: int,
) -> tuple[tuple[int, int], ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or not value
    ):
        raise MazeValidationError("goals must contain at least one cell")
    goals: list[tuple[int, int]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise MazeValidationError(f"goals[{index}] must be an object")
        cell = (
            _bounded_int(item.get("x"), f"goals[{index}].x", 0, cols - 1),
            _bounded_int(item.get("y"), f"goals[{index}].y", 0, rows - 1),
        )
        if cell in goals:
            raise MazeValidationError("goals contain a duplicate cell")
        goals.append(cell)
    return tuple(goals)


def _parse_walls(
    value: Any,
    *,
    rows: int,
    cols: int,
) -> tuple[WallSegment, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
    ):
        raise MazeValidationError("walls must be a list")
    units: list[WallSegment] = []
    seen: set[tuple[str, int, int]] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise MazeValidationError(f"wall {index} must be an object")
        coordinates = []
        for name in ("x1", "y1", "x2", "y2"):
            coordinate = item.get(name)
            if not isinstance(coordinate, int) or isinstance(coordinate, bool):
                raise MazeValidationError(
                    f"wall {index}.{name} must snap to an integer grid line"
                )
            coordinates.append(coordinate)
        x1, y1, x2, y2 = coordinates
        if not (0 <= x1 <= cols and 0 <= x2 <= cols):
            raise MazeValidationError(f"wall {index} x coordinate is out of bounds")
        if not (0 <= y1 <= rows and 0 <= y2 <= rows):
            raise MazeValidationError(f"wall {index} y coordinate is out of bounds")
        if x1 == x2 and y1 == y2:
            raise MazeValidationError(f"wall {index} has zero length")
        if x1 != x2 and y1 != y2:
            raise MazeValidationError(
                f"wall {index} must be horizontal or vertical"
            )
        for unit in _expand_wall(x1, y1, x2, y2):
            key = _wall_key(unit)
            if key in seen:
                raise MazeValidationError(
                    f"duplicate or overlapping wall at {key}"
                )
            seen.add(key)
            units.append(unit)
    return tuple(units)


def _expand_wall(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
) -> tuple[WallSegment, ...]:
    if y1 == y2:
        start, end = sorted((x1, x2))
        return tuple(
            WallSegment(x, y1, x + 1, y1)
            for x in range(start, end)
        )
    start, end = sorted((y1, y2))
    return tuple(
        WallSegment(x1, y, x1, y + 1)
        for y in range(start, end)
    )


def _parse_source_digest(value: Any) -> str | None:
    if value in (None, ""):
        return None
    digest = str(value).lower()
    if not DIGEST_PATTERN.fullmatch(digest):
        raise MazeValidationError(
            "source_image_digest must be a lowercase SHA-256 digest"
        )
    return digest


def _require_closed_boundary(
    walls: set[tuple[str, int, int]],
    *,
    rows: int,
    cols: int,
) -> None:
    required = {
        *(("H", 0, x) for x in range(cols)),
        *(("H", rows, x) for x in range(cols)),
        *(("V", 0, y) for y in range(rows)),
        *(("V", cols, y) for y in range(rows)),
    }
    if not required <= walls:
        raise MazeValidationError("outer boundary must be closed")


def _require_reachable_goal(
    walls: set[tuple[str, int, int]],
    *,
    rows: int,
    cols: int,
    start: tuple[int, int],
    goals: set[tuple[int, int]],
) -> None:
    queue = deque((start,))
    visited = {start}
    while queue:
        cell = queue.popleft()
        if cell in goals:
            return
        for neighbor, wall in _neighbors(cell):
            x, y = neighbor
            if not (0 <= x < cols and 0 <= y < rows):
                continue
            if wall in walls or neighbor in visited:
                continue
            visited.add(neighbor)
            queue.append(neighbor)
    raise MazeValidationError(
        "at least one goal must be reachable from start"
    )


def _neighbors(
    cell: tuple[int, int],
) -> tuple[tuple[tuple[int, int], tuple[str, int, int]], ...]:
    x, y = cell
    return (
        ((x, y - 1), ("H", y, x)),
        ((x + 1, y), ("V", x + 1, y)),
        ((x, y + 1), ("H", y + 1, x)),
        ((x - 1, y), ("V", x, y)),
    )


def _wall_key(wall: WallSegment) -> tuple[str, int, int]:
    if wall.y1 == wall.y2:
        return "H", wall.y1, wall.x1
    return "V", wall.x1, wall.y1


def _wall_sort_key(wall: WallSegment) -> tuple[int, int, int]:
    orientation, fixed, offset = _wall_key(wall)
    return (0 if orientation == "H" else 1, fixed, offset)
