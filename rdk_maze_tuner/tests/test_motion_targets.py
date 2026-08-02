import pytest

from rdk_maze_tuner.core.maze_map import Direction, MazeMap, PlannedAction
from rdk_maze_tuner.core.motion_targets import (
    MotionTargetError,
    MotionTargetResolver,
)
from rdk_maze_tuner.core.motion_evidence import RecoverySuggestion


def resolver(**overrides):
    values = {
        "ticks_per_mm": 5.4,
        "fallback_cell_size_mm": 250.0,
        "turn_90_ticks": 720,
        "turn_180_ticks": 1440,
    }
    values.update(overrides)
    return MotionTargetResolver(**values)


def maze_with_size(
    *,
    width_mm: int | None,
    height_mm: int | None,
    heading: Direction = Direction.NORTH,
) -> MazeMap:
    maze = MazeMap(wall_threshold_mm=150, heading=heading)
    maze.cell_width_mm = width_mm
    maze.cell_height_mm = height_mm
    return maze


def test_250_mm_at_5_4_ticks_per_mm_resolves_to_1350_ticks():
    target = resolver().resolve(
        PlannedAction("move_cell", Direction.NORTH),
        maze_with_size(width_mm=250, height_mm=250),
    )

    assert target.distance_mm == 250.0
    assert target.ticks_per_mm == 5.4
    assert target.target_ticks == 1350
    assert target.direction == "N"
    assert target.source == "map.cell_height_mm"


def test_450_mm_resolves_to_2430_ticks():
    target = resolver().resolve(
        PlannedAction("move_cell", Direction.EAST),
        maze_with_size(width_mm=450, height_mm=250),
    )

    assert target.distance_mm == 450.0
    assert target.target_ticks == 2430
    assert target.source == "map.cell_width_mm"


@pytest.mark.parametrize(
    ("direction", "expected_distance", "expected_source"),
    [
        (Direction.NORTH, 300.0, "map.cell_height_mm"),
        (Direction.SOUTH, 300.0, "map.cell_height_mm"),
        (Direction.EAST, 420.0, "map.cell_width_mm"),
        (Direction.WEST, 420.0, "map.cell_width_mm"),
    ],
)
def test_cardinal_direction_selects_height_or_width(
    direction,
    expected_distance,
    expected_source,
):
    target = resolver().resolve(
        PlannedAction("move_cell", direction),
        maze_with_size(width_mm=420, height_mm=300),
    )

    assert target.distance_mm == expected_distance
    assert target.source == expected_source


def test_move_without_explicit_direction_uses_current_heading():
    target = resolver().resolve(
        PlannedAction("move_cell"),
        maze_with_size(
            width_mm=450,
            height_mm=300,
            heading=Direction.WEST,
        ),
    )

    assert target.direction == "W"
    assert target.target_ticks == 2430


def test_missing_map_dimensions_fall_back_to_robot_cell_size_only():
    target = resolver().resolve(
        PlannedAction("move_cell", Direction.SOUTH),
        maze_with_size(width_mm=None, height_mm=None),
    )

    assert target.distance_mm == 250.0
    assert target.target_ticks == 1350
    assert target.source == "robot.cell_size_cm"


@pytest.mark.parametrize(
    ("name", "expected_ticks", "angle"),
    [
        ("turn_left", 720, -90.0),
        ("turn_right", 720, 90.0),
        ("turn_back", 1440, 180.0),
    ],
)
def test_turns_keep_explicit_calibrated_ticks(name, expected_ticks, angle):
    target = resolver().resolve(
        PlannedAction(name),
        maze_with_size(width_mm=900, height_mm=900),
    )

    assert target.distance_mm is None
    assert target.target_angle_deg == angle
    assert target.target_ticks == expected_ticks
    assert target.source == "motion.turn_calibration"


@pytest.mark.parametrize(
    "overrides",
    [
        {"ticks_per_mm": 0},
        {"ticks_per_mm": -1},
        {"fallback_cell_size_mm": 0},
        {"turn_90_ticks": 0},
        {"turn_180_ticks": -1},
    ],
)
def test_non_positive_configuration_is_rejected(overrides):
    with pytest.raises(MotionTargetError):
        resolver(**overrides)


def test_non_positive_map_distance_and_integer_overflow_are_rejected():
    with pytest.raises(MotionTargetError, match="distance"):
        resolver().resolve(
            PlannedAction("move_cell", Direction.NORTH),
            maze_with_size(width_mm=250, height_mm=0),
        )

    with pytest.raises(MotionTargetError, match="32-bit"):
        resolver(ticks_per_mm=10_000_000).resolve(
            PlannedAction("move_cell", Direction.NORTH),
            maze_with_size(width_mm=250, height_mm=450),
        )


def test_unknown_action_is_rejected():
    with pytest.raises(MotionTargetError, match="unsupported"):
        resolver().resolve(
            PlannedAction("teleport"),
            maze_with_size(width_mm=250, height_mm=250),
        )


def test_nudge_recovery_is_clamped_to_one_quarter_cell_and_half_speed():
    target = resolver().resolve_recovery(
        RecoverySuggestion(
            kind="nudge_forward",
            remaining_distance_mm=120.0,
            max_distance_mm=100.0,
        ),
        maze_with_size(width_mm=420, height_mm=300),
    )

    assert target.action_name == "nudge_forward"
    assert target.distance_mm == 75.0
    assert target.target_ticks == 405
    assert target.source == "recovery.nudge_forward"
    assert resolver().recovery_speed(
        target,
        base_speed=0.25,
        turn_speed=0.18,
    ) == pytest.approx(0.10)


@pytest.mark.parametrize(
    ("delta", "direction"),
    [(-20.0, "left"), (9.5, "right")],
)
def test_heading_recovery_is_directional_and_clamped_to_15_degrees(
    delta,
    direction,
):
    target = resolver().resolve_recovery(
        RecoverySuggestion(
            kind="align_heading",
            heading_delta_deg=delta,
            max_heading_deg=15.0,
        ),
        maze_with_size(width_mm=250, height_mm=250),
    )

    assert target.action_name == "align_heading"
    assert target.direction == direction
    assert abs(target.target_angle_deg) <= 15.0
    assert target.target_ticks <= 120
    assert resolver().recovery_speed(
        target,
        base_speed=0.25,
        turn_speed=0.18,
    ) == pytest.approx(0.09)
