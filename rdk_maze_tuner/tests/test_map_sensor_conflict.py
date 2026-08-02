from __future__ import annotations

from math import nan

from rdk_maze_tuner.core.map_sensor_conflict import (
    MAP_SENSOR_CONFLICT,
    MapSensorConflictDetector,
)
from rdk_maze_tuner.core.maze_map import Direction


def wall_sample(
    detector: MapSensorConflictDetector,
    *,
    direction: Direction = Direction.NORTH,
    distance_mm: float | None = 90,
    planned_wall: bool | None = False,
):
    return detector.observe(
        cell=(0, 1),
        direction=direction,
        planned_wall=planned_wall,
        distance_mm=distance_mm,
        wall_threshold_mm=150,
    )


def test_third_consecutive_valid_wall_sample_latches_conflict():
    detector = MapSensorConflictDetector(
        required_consecutive_samples=3,
        max_valid_distance_mm=1000,
    )
    detector.reset(run_id="run-1")

    assert wall_sample(detector) is None
    assert wall_sample(detector) is None
    conflict = wall_sample(detector)

    assert conflict is not None
    assert conflict.code == MAP_SENSOR_CONFLICT
    assert conflict.run_id == "run-1"
    assert conflict.cell == (0, 1)
    assert conflict.direction == Direction.NORTH
    assert conflict.sample_count == 3
    assert conflict.distance_mm == 90
    assert conflict.to_dict()["code"] == "MAP_SENSOR_CONFLICT"


def test_invalid_overrange_open_and_different_direction_samples_do_not_accumulate():
    detector = MapSensorConflictDetector(
        required_consecutive_samples=3,
        max_valid_distance_mm=1000,
    )
    detector.reset(run_id="run-1")

    assert wall_sample(detector) is None
    assert wall_sample(detector, distance_mm=None) is None
    assert wall_sample(detector, distance_mm=nan) is None
    assert wall_sample(detector, distance_mm=0) is None
    assert wall_sample(detector, distance_mm=1001) is None
    assert wall_sample(detector) is None
    assert wall_sample(detector, direction=Direction.EAST) is None
    assert wall_sample(detector) is None
    assert wall_sample(detector) is None
    assert wall_sample(detector, distance_mm=500) is None
    assert wall_sample(detector) is None
    assert wall_sample(detector) is None
    assert wall_sample(detector, planned_wall=True) is None
    assert wall_sample(detector) is None
    assert wall_sample(detector) is None
    assert wall_sample(detector) is not None


def test_conflict_stays_latched_until_explicit_task_reset():
    detector = MapSensorConflictDetector(required_consecutive_samples=3)
    detector.reset(run_id="run-1")
    for _ in range(2):
        assert wall_sample(detector) is None
    first = wall_sample(detector)
    assert first is not None

    assert wall_sample(detector, distance_mm=500) is first
    detector.reset(run_id="run-2")

    assert detector.latched_conflict is None
    assert wall_sample(detector) is None
    assert wall_sample(detector) is None
    second = wall_sample(detector)
    assert second is not None
    assert second.run_id == "run-2"
