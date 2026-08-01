from copy import deepcopy

import pytest

from rdk_maze_tuner.core.maze_definition import MapDefinition
from rdk_maze_tuner.core.maze_map import Direction, MazeMap
from rdk_maze_tuner.core.maze_validation import (
    MazeValidationError,
    validate_map_definition,
)


def valid_definition() -> dict:
    return {
        "rows": 2,
        "cols": 2,
        "cell_width_mm": 300,
        "cell_height_mm": 300,
        "wall_thickness_mm": 18,
        "wall_height_mm": 120,
        "start": {"x": 0, "y": 1, "heading": "N"},
        "goals": [{"x": 1, "y": 0}],
        "walls": [
            {"x1": 0, "y1": 0, "x2": 2, "y2": 0},
            {"x1": 2, "y1": 0, "x2": 2, "y2": 2},
            {"x1": 2, "y1": 2, "x2": 0, "y2": 2},
            {"x1": 0, "y1": 2, "x2": 0, "y2": 0},
            {"x1": 1, "y1": 0, "x2": 1, "y2": 1},
        ],
        "source_image_digest": None,
    }


def test_map_definition_normalizes_walls_and_has_stable_content_digest():
    first = valid_definition()
    equivalent = deepcopy(first)
    equivalent["walls"] = [
        {"x1": 1, "y1": 1, "x2": 1, "y2": 0},
        {"x1": 0, "y1": 0, "x2": 1, "y2": 0},
        {"x1": 1, "y1": 0, "x2": 2, "y2": 0},
        {"x1": 2, "y1": 2, "x2": 2, "y2": 0},
        {"x1": 2, "y1": 2, "x2": 0, "y2": 2},
        {"x1": 0, "y1": 2, "x2": 0, "y2": 0},
    ]

    first_map = validate_map_definition(first)
    second_map = validate_map_definition(equivalent)

    assert isinstance(first_map, MapDefinition)
    assert first_map.content_digest == second_map.content_digest
    assert first_map.to_dict() == second_map.to_dict()
    assert all(
        abs(wall["x2"] - wall["x1"]) + abs(wall["y2"] - wall["y1"])
        == 1
        for wall in first_map.to_dict()["walls"]
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rows", 0),
        ("cols", 65),
        ("cell_width_mm", 80),
        ("cell_height_mm", 5001),
        ("wall_thickness_mm", 0),
        ("wall_height_mm", 0),
    ],
)
def test_map_definition_rejects_invalid_dimensions(field, value):
    payload = valid_definition()
    payload[field] = value

    with pytest.raises(MazeValidationError, match=field):
        validate_map_definition(payload)


@pytest.mark.parametrize(
    "wall",
    [
        {"x1": 0, "y1": 0, "x2": 0, "y2": 0},
        {"x1": 0, "y1": 0, "x2": 1, "y2": 1},
        {"x1": -1, "y1": 0, "x2": 0, "y2": 0},
        {"x1": 0, "y1": 0, "x2": 3, "y2": 0},
    ],
)
def test_map_definition_rejects_unsnapped_zero_or_out_of_bounds_walls(wall):
    payload = valid_definition()
    payload["walls"].append(wall)

    with pytest.raises(MazeValidationError, match="wall"):
        validate_map_definition(payload)


def test_map_definition_rejects_duplicate_or_overlapping_wall_geometry():
    payload = valid_definition()
    payload["walls"].append(
        {"x1": 0, "y1": 0, "x2": 1, "y2": 0}
    )

    with pytest.raises(MazeValidationError, match="duplicate"):
        validate_map_definition(payload)


def test_map_definition_requires_closed_outer_boundary():
    payload = valid_definition()
    payload["walls"] = payload["walls"][1:]

    with pytest.raises(MazeValidationError, match="outer boundary"):
        validate_map_definition(payload)


def test_map_definition_requires_reachable_goal():
    payload = valid_definition()
    payload["walls"].append(
        {"x1": 0, "y1": 1, "x2": 2, "y2": 1}
    )

    with pytest.raises(MazeValidationError, match="reachable"):
        validate_map_definition(payload)


def test_structured_definition_builds_maze_map_with_screen_coordinates():
    definition = validate_map_definition(valid_definition())

    maze = MazeMap.from_definition(
        definition,
        wall_threshold_mm=150,
        map_version_id="mapv-001",
    )

    assert maze.position == (0, 1)
    assert maze.heading == Direction.NORTH
    assert maze.neighbor((0, 1), Direction.NORTH) == (0, 0)
    assert maze.cell((0, 0)).walls["E"] is True
    assert maze.cell((0, 1)).walls["E"] is False
    snapshot = maze.to_dict()
    assert snapshot["map_version_id"] == "mapv-001"
    assert snapshot["map_digest"] == definition.content_digest
