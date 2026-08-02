"""DFS maze planner that waits for ESP32 action completion."""

from __future__ import annotations

from typing import Iterable, List

from .maze_map import Direction, MazeMap, PlannedAction


class MazePlanner:
    def __init__(self, priority: Iterable[str] = ("N", "E", "W", "S")) -> None:
        self.priority = tuple(Direction(item) for item in priority)
        self.stack: List[tuple[int, int]] = []
        self.pending: List[PlannedAction] = []

    def next_action(self, maze: MazeMap) -> PlannedAction:
        if self.pending:
            return self.pending.pop(0)

        for direction in self.priority:
            if maze.wall_for_planning(maze.position, direction) is not False:
                continue
            target = maze.neighbor(maze.position, direction)
            if target in maze.visited:
                continue
            self.stack.append(maze.position)
            self.pending.extend(self._plan_to_direction(maze, direction))
            return self.pending.pop(0)

        if self.stack:
            target = self.stack.pop()
            direction = self._direction_to_neighbor(maze, target)
            self.pending.extend(self._plan_to_direction(maze, direction))
            return self.pending.pop(0)

        return PlannedAction("stop")

    def _plan_to_direction(self, maze: MazeMap, direction: Direction) -> List[PlannedAction]:
        current = _heading_index(maze.heading)
        desired = _heading_index(direction)
        delta = (desired - current) % 4
        if delta == 0:
            return [PlannedAction("move_cell", direction)]
        if delta == 1:
            return [PlannedAction("turn_right"), PlannedAction("move_cell", direction)]
        if delta == 3:
            return [PlannedAction("turn_left"), PlannedAction("move_cell", direction)]
        return [PlannedAction("turn_back"), PlannedAction("move_cell", direction)]

    def _direction_to_neighbor(self, maze: MazeMap, target: tuple[int, int]) -> Direction:
        px, py = maze.position
        tx, ty = target
        delta = (tx - px, ty - py)
        for direction in Direction:
            if maze.neighbor(maze.position, direction) == target:
                return direction
        raise ValueError(f"{target} is not adjacent to {maze.position}; delta={delta}")


def _heading_index(direction: Direction) -> int:
    return {
        Direction.NORTH: 0,
        Direction.EAST: 1,
        Direction.SOUTH: 2,
        Direction.WEST: 3,
    }[direction]
