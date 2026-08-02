"""Deterministic shortest-path planner for immutable map-owned goals."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Iterable

from .maze_map import Coord, Direction, MazeMap, PlannedAction


NO_PATH = "NO_PATH"


class GoalPlanningError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class GoalRoute:
    start: Coord
    start_heading: Direction
    goal: Coord
    cells: tuple[Coord, ...]
    actions: tuple[PlannedAction, ...]
    map_version_id: str | None
    map_digest: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "map_version_id": self.map_version_id,
            "map_digest": self.map_digest,
            "start": [self.start[0], self.start[1]],
            "start_heading": self.start_heading.value,
            "goal": [self.goal[0], self.goal[1]],
            "cells": [[cell[0], cell[1]] for cell in self.cells],
            "actions": [
                {
                    "name": action.name,
                    "direction": (
                        action.direction.value
                        if action.direction is not None
                        else None
                    ),
                }
                for action in self.actions
            ],
            "cell_count": max(0, len(self.cells) - 1),
            "action_count": len(self.actions),
        }


class GoalDirectedPlanner:
    """Replan from the current reliable pose on every action boundary."""

    def __init__(
        self,
        *,
        goal_cells: Iterable[Coord],
        priority: Iterable[str | Direction] = ("N", "E", "S", "W"),
    ) -> None:
        normalized_goals = {
            _normalize_cell(cell, "goal")
            for cell in goal_cells
        }
        if not normalized_goals:
            raise ValueError("goal_cells must contain at least one cell")
        self.goal_cells = tuple(
            sorted(normalized_goals, key=lambda cell: (cell[1], cell[0]))
        )
        self.priority = tuple(Direction(item) for item in priority)
        if set(self.priority) != set(Direction) or len(self.priority) != 4:
            raise ValueError("priority must contain N, E, S and W exactly once")
        self.last_route: GoalRoute | None = None
        self.last_error: GoalPlanningError | None = None
        self._pending_route_event: GoalRoute | None = None

    def plan_route(self, maze: MazeMap) -> GoalRoute:
        rows, cols = _validated_dimensions(maze)
        start = _normalize_cell(maze.position, "current position")
        if not _in_bounds(start, rows=rows, cols=cols):
            raise GoalPlanningError(
                NO_PATH,
                "current reliable position is outside the map",
            )
        goals = tuple(
            cell
            for cell in self.goal_cells
            if _in_bounds(cell, rows=rows, cols=cols)
        )
        if not goals:
            raise GoalPlanningError(
                NO_PATH,
                "no configured goal is inside the map",
            )

        parents: dict[Coord, Coord | None] = {start: None}
        distances: dict[Coord, int] = {start: 0}
        pending = deque([start])
        while pending:
            current = pending.popleft()
            for direction in self.priority:
                if maze.wall_for_planning(current, direction) is not False:
                    continue
                neighbor = maze.neighbor(current, direction)
                if not _in_bounds(neighbor, rows=rows, cols=cols):
                    continue
                if neighbor in parents:
                    continue
                parents[neighbor] = current
                distances[neighbor] = distances[current] + 1
                pending.append(neighbor)

        reachable = [
            (distances[cell], cell[1], cell[0], cell)
            for cell in goals
            if cell in distances
        ]
        if not reachable:
            raise GoalPlanningError(
                NO_PATH,
                "no legal route reaches the map-owned goal",
            )
        _distance, _y, _x, goal = min(reachable)
        cells = _reconstruct_cells(parents, goal)
        actions = _cells_to_actions(
            maze,
            cells,
            start_heading=maze.heading,
        )
        route = GoalRoute(
            start=start,
            start_heading=maze.heading,
            goal=goal,
            cells=cells,
            actions=actions,
            map_version_id=maze.map_version_id,
            map_digest=maze.map_digest,
        )
        self.last_route = route
        self.last_error = None
        return route

    def next_action(self, maze: MazeMap) -> PlannedAction:
        try:
            route = self.plan_route(maze)
        except GoalPlanningError as exc:
            self.last_route = None
            self.last_error = exc
            self._pending_route_event = None
            return PlannedAction("stop")
        self._pending_route_event = route
        if not route.actions:
            return PlannedAction("stop")
        return route.actions[0]

    def consume_route_event(self) -> dict[str, object] | None:
        route = self._pending_route_event
        self._pending_route_event = None
        return None if route is None else route.to_dict()


def _normalize_cell(value: object, label: str) -> Coord:
    if (
        not isinstance(value, (tuple, list))
        or len(value) != 2
        or not all(
            isinstance(item, int) and not isinstance(item, bool)
            for item in value
        )
    ):
        raise ValueError(f"{label} must be an integer (x, y) cell")
    return int(value[0]), int(value[1])


def _validated_dimensions(maze: MazeMap) -> tuple[int, int]:
    if (
        not isinstance(maze.rows, int)
        or isinstance(maze.rows, bool)
        or maze.rows < 1
        or not isinstance(maze.cols, int)
        or isinstance(maze.cols, bool)
        or maze.cols < 1
    ):
        raise GoalPlanningError(
            NO_PATH,
            "goal-directed planning requires bounded map dimensions",
        )
    return maze.rows, maze.cols


def _in_bounds(cell: Coord, *, rows: int, cols: int) -> bool:
    return 0 <= cell[0] < cols and 0 <= cell[1] < rows


def _reconstruct_cells(
    parents: dict[Coord, Coord | None],
    goal: Coord,
) -> tuple[Coord, ...]:
    reverse_path = [goal]
    current = goal
    while parents[current] is not None:
        current = parents[current]
        reverse_path.append(current)
    return tuple(reversed(reverse_path))


def _cells_to_actions(
    maze: MazeMap,
    cells: tuple[Coord, ...],
    *,
    start_heading: Direction,
) -> tuple[PlannedAction, ...]:
    heading = start_heading
    actions: list[PlannedAction] = []
    for current, neighbor in zip(cells, cells[1:]):
        direction = _direction_between(maze, current, neighbor)
        turns, heading = _turn_actions(heading, direction)
        actions.extend(turns)
        actions.append(PlannedAction("move_cell", direction))
    return tuple(actions)


def _direction_between(
    maze: MazeMap,
    current: Coord,
    neighbor: Coord,
) -> Direction:
    for direction in Direction:
        if maze.neighbor(current, direction) == neighbor:
            return direction
    raise GoalPlanningError(
        NO_PATH,
        f"route contains non-adjacent cells: {current} -> {neighbor}",
    )


def _turn_actions(
    current: Direction,
    desired: Direction,
) -> tuple[tuple[PlannedAction, ...], Direction]:
    order = (
        Direction.NORTH,
        Direction.EAST,
        Direction.SOUTH,
        Direction.WEST,
    )
    delta = (order.index(desired) - order.index(current)) % 4
    if delta == 0:
        return (), desired
    if delta == 1:
        return (PlannedAction("turn_right"),), desired
    if delta == 3:
        return (PlannedAction("turn_left"),), desired
    return (PlannedAction("turn_back"),), desired
