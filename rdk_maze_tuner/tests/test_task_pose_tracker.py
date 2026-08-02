from __future__ import annotations

import pytest

from rdk_maze_tuner.core.maze_definition import (
    MapDefinition,
    StartPose,
    WallSegment,
)
from rdk_maze_tuner.core.maze_map import Direction, MazeMap, PlannedAction
from rdk_maze_tuner.core.motion_evidence import (
    POSE_UNCERTAIN,
    ArrivalVerificationConfig,
)
from rdk_maze_tuner.core.motion_targets import MotionTarget
from rdk_maze_tuner.core.param_manager import ParamManager
from rdk_maze_tuner.core.task_pose_tracker import (
    ACTION_RESULT_INVALID,
    ACTION_RESULT_MISMATCH,
    TaskPoseTracker,
    TaskPoseTrackerError,
)


PARAMS = "rdk_maze_tuner/config/params.yaml"
LIMITS = "rdk_maze_tuner/config/limits.yaml"


def corridor_definition() -> MapDefinition:
    rows = 2
    cols = 1
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
        goals=((0, 0),),
        walls=boundary,
    )


def long_corridor_definition() -> MapDefinition:
    rows = 1
    cols = 5
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
        start=StartPose(x=0, y=0, heading="E"),
        goals=((4, 0),),
        walls=boundary,
    )


def tracker(
    *,
    conflict_required_samples: int = 3,
) -> tuple[TaskPoseTracker, MazeMap]:
    params = ParamManager(
        params_path=__import__("pathlib").Path(PARAMS),
        limits_path=__import__("pathlib").Path(LIMITS),
    )
    maze = MazeMap.from_definition(
        corridor_definition(),
        wall_threshold_mm=150,
        map_version_id="corridor-v1",
    )
    return (
        TaskPoseTracker.from_params(
            maze=maze,
            params=params,
            arrival_config=ArrivalVerificationConfig(),
            run_id="run-pose-1",
            conflict_required_samples=conflict_required_samples,
        ),
        maze,
    )


def move_target() -> MotionTarget:
    return MotionTarget(
        action_name="move_cell",
        direction="N",
        distance_mm=450.0,
        ticks_per_mm=5.4,
        target_ticks=2430,
        source="map.cell_height_mm",
    )


def baseline_telemetry(**overrides):
    value = {
        "type": "telemetry",
        "ts_ms": 0,
        "enc_left": 0,
        "enc_right": 0,
        "front_mm": 675.0,
        "left_mm": 225.0,
        "right_mm": 225.0,
        "imu_available": False,
    }
    value.update(overrides)
    return value


def done_message(**overrides):
    value = {
        "type": "done",
        "action_id": "move-0001",
        "name": "move_cell",
        "success": True,
        "duration_ms": 1000,
        "enc_left": 2430,
        "enc_right": 2430,
        "front_mm": 225.0,
        "left_mm": 225.0,
        "right_mm": 225.0,
        "imu_available": False,
    }
    value.update(overrides)
    return value


def test_action_baseline_is_run_scoped_and_keeps_encoders_and_fused_pose():
    pose_tracker, maze = tracker()
    action = PlannedAction("move_cell", Direction.NORTH)

    baseline = pose_tracker.begin_action(
        action_id="move-0001",
        action=action,
        telemetry=baseline_telemetry(),
    )

    assert baseline.action_id == "move-0001"
    assert baseline.enc_left == 0
    assert baseline.enc_right == 0
    assert baseline.pose.grid_cell == maze.position
    assert baseline.pose.heading == maze.heading.value
    assert pose_tracker.last_baseline is baseline


def test_only_matching_done_or_error_can_close_the_active_action():
    pose_tracker, _maze = tracker()
    action = PlannedAction("move_cell", Direction.NORTH)
    pose_tracker.begin_action(
        action_id="move-0001",
        action=action,
        telemetry=baseline_telemetry(),
    )

    with pytest.raises(TaskPoseTrackerError) as mismatch:
        pose_tracker.complete_action(
            action_id="move-0001",
            action=action,
            result=done_message(action_id="other-action"),
            motion_target=move_target(),
        )
    assert mismatch.value.code == ACTION_RESULT_MISMATCH

    with pytest.raises(TaskPoseTrackerError) as invalid:
        pose_tracker.complete_action(
            action_id="move-0001",
            action=action,
            result={"type": "ack", "action_id": "move-0001"},
            motion_target=move_target(),
        )
    assert invalid.value.code == ACTION_RESULT_INVALID


def test_wall_distance_change_is_external_motion_and_qualifies_no_imu_pose():
    pose_tracker, maze = tracker()
    action = PlannedAction("move_cell", Direction.NORTH)
    pose_tracker.begin_action(
        action_id="move-0001",
        action=action,
        telemetry=baseline_telemetry(),
    )

    tracked = pose_tracker.complete_action(
        action_id="move-0001",
        action=action,
        result=done_message(),
        motion_target=move_target(),
    )

    assert tracked.evidence.encoder_displacement_mm == pytest.approx(450.0)
    assert tracked.evidence.external_displacement_mm == pytest.approx(450.0)
    assert tracked.external_evidence_available is True
    assert tracked.independent_wall_constraints == 2
    assert tracked.pose.confidence >= 0.80
    assert tracked.decision.status == "accepted"
    assert maze.position == (0, 1)

    reliable = pose_tracker.accept_action(action)
    assert reliable.grid_cell == (0, 0)
    assert maze.position == (0, 1)


def test_encoder_is_never_reused_as_external_motion_without_longitudinal_wall():
    pose_tracker, _maze = tracker()
    action = PlannedAction("move_cell", Direction.NORTH)
    pose_tracker.begin_action(
        action_id="move-0001",
        action=action,
        telemetry=baseline_telemetry(
            front_mm=None,
            imu_available=True,
            imu_yaw_deg=0.0,
        ),
    )

    tracked = pose_tracker.complete_action(
        action_id="move-0001",
        action=action,
        result=done_message(
            front_mm=None,
            imu_available=True,
            imu_yaw_deg=0.0,
        ),
        motion_target=move_target(),
    )

    assert tracked.external_evidence_available is False
    assert tracked.evidence.external_displacement_mm == 0.0
    assert tracked.evidence.encoder_displacement_mm == pytest.approx(450.0)
    assert tracked.decision.status == "accepted"
    assert tracked.decision.code is None


def test_no_imu_needs_success_stable_heading_and_two_independent_wall_axes():
    pose_tracker, _maze = tracker()
    action = PlannedAction("move_cell", Direction.NORTH)
    pose_tracker.begin_action(
        action_id="move-0001",
        action=action,
        telemetry=baseline_telemetry(left_mm=None, right_mm=None),
    )

    tracked = pose_tracker.complete_action(
        action_id="move-0001",
        action=action,
        result=done_message(left_mm=None, right_mm=None),
        motion_target=move_target(),
    )

    assert tracked.independent_wall_constraints == 1
    assert tracked.pose.confidence < 0.80
    assert tracked.decision.status == "unsafe"
    assert tracked.decision.code == POSE_UNCERTAIN


def test_turn_heading_is_only_anchored_after_heading_evidence_is_accepted():
    pose_tracker, maze = tracker()
    action = PlannedAction("turn_left")
    target = MotionTarget(
        action_name="turn_left",
        direction=None,
        distance_mm=None,
        ticks_per_mm=3.0,
        target_ticks=720,
        source="motion.turn_calibration",
        target_angle_deg=-90.0,
    )
    pose_tracker.begin_action(
        action_id="turn-0001",
        action=action,
        telemetry=baseline_telemetry(
            imu_available=True,
            imu_yaw_deg=0.0,
        ),
    )

    tracked = pose_tracker.complete_action(
        action_id="turn-0001",
        action=action,
        result={
            **done_message(
                action_id="turn-0001",
                name="turn_left",
                enc_left=-720,
                enc_right=720,
                imu_available=True,
                imu_yaw_deg=270.0,
                front_mm=225.0,
                left_mm=675.0,
                right_mm=225.0,
            ),
        },
        motion_target=target,
    )

    assert tracked.decision.status == "accepted"
    assert maze.heading == Direction.NORTH
    assert tracked.pose.heading == "N"
    accepted = pose_tracker.accept_action(action)
    assert accepted.heading == "W"
    assert maze.heading == Direction.NORTH


def test_sim_truth_is_absent_from_tracker_inputs_and_motion_evidence():
    pose_tracker, _maze = tracker()
    action = PlannedAction("move_cell", Direction.NORTH)
    pose_tracker.begin_action(
        action_id="move-0001",
        action=action,
        telemetry=baseline_telemetry(
            sim_truth={"x_mm": 999999, "y_mm": -999999, "yaw_deg": 179}
        ),
    )
    tracked = pose_tracker.complete_action(
        action_id="move-0001",
        action=action,
        result=done_message(
            sim_truth={"x_mm": -1, "y_mm": -1, "yaw_deg": 1}
        ),
        motion_target=move_target(),
    )

    assert "sim_truth" not in pose_tracker.last_fusion_input
    assert "sim_truth" not in str(tracked.to_dict())


def test_map_scale_does_not_redefine_encoder_calibration_or_fake_slip():
    params = ParamManager(
        params_path=__import__("pathlib").Path(PARAMS),
        limits_path=__import__("pathlib").Path(LIMITS),
    )
    maze = MazeMap.from_definition(
        long_corridor_definition(),
        wall_threshold_mm=150,
        map_version_id="long-corridor-v1",
    )
    pose_tracker = TaskPoseTracker.from_params(
        maze=maze,
        params=params,
        arrival_config=ArrivalVerificationConfig(),
        run_id="run-map-scale",
    )
    action = PlannedAction("move_cell", Direction.EAST)
    target = MotionTarget(
        action_name="move_cell",
        direction="E",
        distance_mm=450.0,
        ticks_per_mm=5.4,
        target_ticks=2430,
        source="map.cell_width_mm",
    )
    baseline = {
        "type": "telemetry",
        "ts_ms": 0,
        "enc_left": 0,
        "enc_right": 0,
        "front_mm": 2000.0,
        "left_mm": 225.0,
        "right_mm": 225.0,
        "tof_max_range_mm": 2000.0,
        "imu_available": True,
        "imu_yaw_deg": 90.0,
    }
    pose_tracker.begin_action(
        action_id="move-scaled",
        action=action,
        telemetry=baseline,
    )
    tracked = pose_tracker.complete_action(
        action_id="move-scaled",
        action=action,
        result={
            **baseline,
            "type": "done",
            "action_id": "move-scaled",
            "name": "move_cell",
            "success": True,
            "duration_ms": 3000,
            "ts_ms": 3000,
            "enc_left": 2430,
            "enc_right": 2430,
        },
        motion_target=target,
    )

    assert pose_tracker.fusion.config.mm_per_tick == pytest.approx(
        250.0 / 1350.0
    )
    assert tracked.evidence.encoder_displacement_mm == pytest.approx(450.0)
    assert tracked.external_evidence_available is False
    assert tracked.decision.status == "accepted"
    assert tracked.decision.code is None
    assert tracked.pose.x_mm == pytest.approx(675.0, abs=30.0)
