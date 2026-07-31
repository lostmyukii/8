"""Grid maze state and action application."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Tuple


Coord = Tuple[int, int]


class Direction(str, Enum):
    NORTH = "N"
    EAST = "E"
    SOUTH = "S"
    WEST = "W"


ORDER = [Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST]
DELTAS = {
    Direction.NORTH: (0, 1),
    Direction.SOUTH: (0, -1),
    Direction.EAST: (1, 0),
    Direction.WEST: (-1, 0),
}
OPPOSITE = {
    Direction.NORTH: Direction.SOUTH,
    Direction.SOUTH: Direction.NORTH,
    Direction.EAST: Direction.WEST,
    Direction.WEST: Direction.EAST,
}


@dataclass
class Cell:
    coord: Coord
    walls: Dict[str, Optional[bool]] = field(
        default_factory=lambda: {"N": None, "E": None, "S": None, "W": None}
    )
    visited: bool = False


@dataclass(frozen=True)
class PlannedAction:
    name: str
    direction: Optional[Direction] = None


class MazeMap:
    def __init__(self, *, wall_threshold_mm: int, start: Coord = (0, 0), heading: Direction = Direction.NORTH) -> None:
        self.wall_threshold_mm = wall_threshold_mm
        self.position = start
        self.start = start
        self.heading = heading
        self.cells: Dict[Coord, Cell] = {}
        self.visited = {start}
        self.cell(start).visited = True

    def cell(self, coord: Coord) -> Cell:
        if coord not in self.cells:
            self.cells[coord] = Cell(coord=coord)
        return self.cells[coord]

    def observe(self, *, front_mm: int, left_mm: int, right_mm: int) -> None:
        readings = {
            "front": front_mm,
            "left": left_mm,
            "right": right_mm,
        }
        for local_direction, distance in readings.items():
            direction = self.local_to_global(local_direction)
            self.set_wall(self.position, direction, distance < self.wall_threshold_mm)

    def set_wall(self, coord: Coord, direction: Direction, blocked: bool) -> None:
        self.cell(coord).walls[direction.value] = blocked
        neighbor = self.neighbor(coord, direction)
        self.cell(neighbor).walls[OPPOSITE[direction].value] = blocked

    def neighbor(self, coord: Coord, direction: Direction) -> Coord:
        dx, dy = DELTAS[direction]
        return coord[0] + dx, coord[1] + dy

    def local_to_global(self, local_direction: str) -> Direction:
        offset = {"front": 0, "right": 1, "back": 2, "left": 3}[local_direction]
        idx = ORDER.index(self.heading)
        return ORDER[(idx + offset) % 4]

    def apply_completed_action(self, action: PlannedAction) -> None:
        if action.name == "turn_right":
            self.heading = self._turn(1)
            return
        if action.name == "turn_left":
            self.heading = self._turn(-1)
            return
        if action.name == "turn_back":
            self.heading = self._turn(2)
            return
        if action.name == "move_cell":
            direction = action.direction or self.heading
            self.position = self.neighbor(self.position, direction)
            self.visited.add(self.position)
            self.cell(self.position).visited = True
            return
        if action.name == "stop":
            return
        raise ValueError(f"unknown completed action: {action.name}")

    def _turn(self, delta: int) -> Direction:
        idx = ORDER.index(self.heading)
        return ORDER[(idx + delta) % 4]

    def render_ascii(self) -> str:
        xs = [coord[0] for coord in self.cells] or [self.position[0]]
        ys = [coord[1] for coord in self.cells] or [self.position[1]]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)

        lines = []
        heading_mark = {
            Direction.NORTH: "^",
            Direction.EAST: ">",
            Direction.SOUTH: "v",
            Direction.WEST: "<",
        }[self.heading]

        for y in range(max_y, min_y - 1, -1):
            top = "+"
            for x in range(min_x, max_x + 1):
                top += self._hseg(self.cell((x, y)).walls["N"]) + "+"
            lines.append(top)

            mid = ""
            for x in range(min_x, max_x + 1):
                cell = self.cell((x, y))
                mark = " "
                if (x, y) == self.position:
                    mark = heading_mark
                elif (x, y) == self.start:
                    mark = "S"
                elif cell.visited:
                    mark = "."
                mid += f"{self._vseg(cell.walls['W'])} {mark} "
            mid += self._vseg(self.cell((max_x, y)).walls["E"])
            lines.append(mid)

        bottom = "+"
        for x in range(min_x, max_x + 1):
            bottom += self._hseg(self.cell((x, min_y)).walls["S"]) + "+"
        lines.append(bottom)
        return "\n".join(lines)

    def _hseg(self, wall: Optional[bool]) -> str:
        if wall is True:
            return "---"
        if wall is False:
            return "   "
        return " ? "

    def _vseg(self, wall: Optional[bool]) -> str:
        if wall is True:
            return "|"
        if wall is False:
            return " "
        return "?"

    def to_dict(self) -> dict:
        return {
            "position": [self.position[0], self.position[1]],
            "start": [self.start[0], self.start[1]],
            "heading": self.heading.value,
            "wall_threshold_mm": self.wall_threshold_mm,
            "visited": [[x, y] for x, y in sorted(self.visited)],
            "cells": [
                {
                    "coord": [coord[0], coord[1]],
                    "visited": cell.visited,
                    "walls": dict(cell.walls),
                }
                for coord, cell in sorted(self.cells.items())
            ],
            "ascii": self.render_ascii(),
        }
