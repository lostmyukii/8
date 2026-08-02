from __future__ import annotations

from rdk_maze_tuner.core.maze_definition import (
    MapDefinition,
    StartPose,
    WallSegment,
)
from rdk_maze_tuner.core.maze_map import Direction, MazeMap
from rdk_maze_tuner.core.maze_planner import MazePlanner


def planned_maze() -> MazeMap:
    rows = 2
    cols = 2
    walls = (
        *(WallSegment(x, 0, x + 1, 0) for x in range(cols)),
        *(WallSegment(x, rows, x + 1, rows) for x in range(cols)),
        *(WallSegment(0, y, 0, y + 1) for y in range(rows)),
        *(WallSegment(cols, y, cols, y + 1) for y in range(rows)),
        WallSegment(1, 1, 1, 2),
    )
    definition = MapDefinition(
        rows=rows,
        cols=cols,
        cell_width_mm=450,
        cell_height_mm=450,
        wall_thickness_mm=40,
        wall_height_mm=180,
        start=StartPose(x=0, y=1, heading="N"),
        goals=((1, 0),),
        walls=walls,
    )
    return MazeMap.from_definition(definition, wall_threshold_mm=150)


def test_planner_prefers_planned_wall_over_conflicting_open_observation():
    maze = planned_maze()
    planner = MazePlanner(priority=("E", "N", "W", "S"))
    maze.observe(front_mm=500, left_mm=90, right_mm=500)

    action = planner.next_action(maze)

    assert maze.cell((0, 1)).observed_walls["E"] is False
    assert maze.cell((0, 1)).planned_walls["E"] is True
    assert action.name == "move_cell"
    assert action.direction == Direction.NORTH


def test_legacy_dfs_still_uses_observed_open_direction():
    maze = MazeMap(wall_threshold_mm=150)
    planner = MazePlanner(priority=("N", "E", "W", "S"))
    maze.observe(front_mm=500, left_mm=90, right_mm=90)

    action = planner.next_action(maze)

    assert action.name == "move_cell"
    assert action.direction == Direction.NORTH
