from __future__ import annotations

import pytest

from rdk_maze_tuner.core.maze_definition import (
    MapDefinition,
    StartPose,
    WallSegment,
)
from rdk_maze_tuner.core.maze_map import Direction, MazeMap


def map_definition(
    *,
    internal_walls: tuple[WallSegment, ...] = (),
) -> MapDefinition:
    rows = 2
    cols = 2
    boundary = (
        *(WallSegment(x, 0, x + 1, 0) for x in range(cols)),
        *(WallSegment(x, rows, x + 1, rows) for x in range(cols)),
        *(WallSegment(0, y, 0, y + 1) for y in range(rows)),
        *(WallSegment(cols, y, cols, y + 1) for y in range(rows)),
    )
    return MapDefinition(
        rows=rows,
        cols=cols,
        cell_width_mm=450,
        cell_height_mm=450,
        wall_thickness_mm=40,
        wall_height_mm=180,
        start=StartPose(x=0, y=1, heading="N"),
        goals=((1, 0),),
        walls=boundary + internal_walls,
    )


def test_definition_walls_are_immutable_planning_evidence():
    definition = map_definition(
        internal_walls=(WallSegment(1, 1, 1, 2),),
    )
    maze = MazeMap.from_definition(
        definition,
        wall_threshold_mm=150,
        map_version_id="mapv-planned",
    )

    origin = maze.cell((0, 1))
    assert origin.planned_walls == {
        "N": False,
        "E": True,
        "S": True,
        "W": True,
    }
    assert origin.observed_walls == {
        "N": None,
        "E": None,
        "S": None,
        "W": None,
    }
    assert maze.wall_for_planning((0, 1), Direction.EAST) is True
    with pytest.raises(TypeError):
        origin.walls["E"] = False


def test_observation_does_not_overwrite_planned_open_or_blocked_walls():
    definition = map_definition(
        internal_walls=(WallSegment(1, 1, 1, 2),),
    )
    maze = MazeMap.from_definition(definition, wall_threshold_mm=150)

    maze.observe(front_mm=90, left_mm=500, right_mm=500)

    origin = maze.cell((0, 1))
    assert origin.planned_walls["N"] is False
    assert origin.observed_walls["N"] is True
    assert maze.wall_for_planning((0, 1), Direction.NORTH) is False
    assert origin.walls["N"] is False
    assert origin.planned_walls["E"] is True
    assert origin.observed_walls["E"] is False
    assert maze.wall_for_planning((0, 1), Direction.EAST) is True
    assert origin.walls["E"] is True


def test_legacy_observation_only_map_uses_observed_walls_for_planning():
    maze = MazeMap(wall_threshold_mm=150)

    maze.observe(front_mm=500, left_mm=90, right_mm=90)

    cell = maze.cell((0, 0))
    assert all(value is None for value in cell.planned_walls.values())
    assert cell.observed_walls == {
        "N": False,
        "E": True,
        "S": None,
        "W": True,
    }
    assert maze.wall_for_planning((0, 0), Direction.NORTH) is False
    assert maze.wall_for_planning((0, 0), Direction.EAST) is True
    snapshot = next(
        item
        for item in maze.to_dict()["cells"]
        if item["coord"] == [0, 0]
    )
    assert snapshot["planned_walls"] == cell.planned_walls
    assert snapshot["observed_walls"] == cell.observed_walls
