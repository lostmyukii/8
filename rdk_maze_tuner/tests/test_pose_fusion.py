import math
from pathlib import Path

from rdk_maze_tuner.core.maze_map import MazeMap
from rdk_maze_tuner.core.param_manager import ParamManager
from rdk_maze_tuner.core.pose_fusion import PoseFusion
from rdk_maze_tuner.core.pose_types import (
    PoseFusionConfig,
    PoseObservation,
    TruthPose,
    WallConstraint,
    evaluate_pose,
)
from rdk_maze_tuner.core.protocol import (
    extract_fusion_telemetry,
    extract_simulation_truth,
)
from rdk_maze_tuner.dashboard.state import DashboardState
from simulation.webots.maze_car.controllers.maze_sim_controller.sim_engine import (
    MazeSimEngine,
)


PARAMS_PATH = Path("rdk_maze_tuner/config/params.yaml")
LIMITS_PATH = Path("rdk_maze_tuner/config/limits.yaml")


def config() -> PoseFusionConfig:
    return PoseFusionConfig(
        cell_width_mm=300.0,
        cell_height_mm=300.0,
        cell_ticks=1350.0,
        turn_90_ticks=720.0,
        wheel_base_mm=135.0,
        y_axis_down=True,
    )


def observation(
    *,
    ts_ms: int,
    enc_left: int,
    enc_right: int,
    imu_available: bool = False,
    imu_yaw_deg: float | None = None,
    yaw_rate_dps: float | None = None,
) -> PoseObservation:
    return PoseObservation(
        timestamp_ms=ts_ms,
        enc_left=enc_left,
        enc_right=enc_right,
        imu_available=imu_available,
        imu_yaw_deg=imu_yaw_deg,
        yaw_rate_dps=yaw_rate_dps,
    )


def test_encoder_prediction_moves_continuous_pose_without_committing_grid():
    fusion = PoseFusion(
        config=config(),
        initial_cell=(1, 1),
        initial_heading="N",
    )

    fusion.update(observation(ts_ms=0, enc_left=0, enc_right=0))
    estimate = fusion.update(
        observation(ts_ms=1000, enc_left=1350, enc_right=1350)
    )

    assert estimate.grid_cell == (1, 1)
    assert estimate.heading == "N"
    assert estimate.x_mm == 450.0
    assert estimate.y_mm == 150.0
    assert estimate.speed_mm_s == 300.0
    assert estimate.continuous_heading_valid is False
    assert "imu_unavailable" in estimate.quality_flags


def test_imu_corrects_yaw_but_grid_heading_changes_only_on_anchor():
    fusion = PoseFusion(
        config=config(),
        initial_cell=(1, 1),
        initial_heading="N",
    )
    fusion.update(
        observation(
            ts_ms=0,
            enc_left=0,
            enc_right=0,
            imu_available=True,
            imu_yaw_deg=0.0,
            yaw_rate_dps=0.0,
        )
    )

    estimate = fusion.update(
        observation(
            ts_ms=1000,
            enc_left=-720,
            enc_right=720,
            imu_available=True,
            imu_yaw_deg=272.0,
            yaw_rate_dps=-88.0,
        )
    )

    assert estimate.heading == "N"
    assert abs(((estimate.yaw_deg - 272.0 + 180) % 360) - 180) < 3.0
    assert estimate.continuous_heading_valid is True
    anchored = fusion.anchor_grid((1, 1), "W")
    assert anchored.grid_cell == (1, 1)
    assert anchored.heading == "W"
    assert abs(((anchored.yaw_deg - 270.0 + 180) % 360) - 180) < 3.0
    assert anchored.correction_source == "grid_action_done"


def test_known_wall_constraint_corrects_position_and_records_residual():
    fusion = PoseFusion(
        config=config(),
        initial_cell=(0, 0),
        initial_heading="E",
    )
    fusion.update(
        observation(
            ts_ms=0,
            enc_left=0,
            enc_right=0,
            imu_available=True,
            imu_yaw_deg=90.0,
        )
    )

    estimate = fusion.update(
        observation(
            ts_ms=100,
            enc_left=0,
            enc_right=0,
            imu_available=True,
            imu_yaw_deg=90.0,
        ),
        wall_constraints=(
            WallConstraint(
                direction="E",
                wall_coordinate_mm=300.0,
                distance_mm=120.0,
                variance_mm2=100.0,
            ),
        ),
    )

    assert 150.0 < estimate.x_mm < 181.0
    assert estimate.wall_residual_mm == 30.0
    assert estimate.correction_source == "tof_wall_E"
    assert estimate.confidence > 0.5


def test_simulation_truth_is_excluded_from_fusion_and_evaluated_separately():
    message = {
        "type": "telemetry",
        "ts_ms": 100,
        "enc_left": 20,
        "enc_right": 20,
        "imu_available": True,
        "imu_yaw_deg": 0.0,
        "yaw_rate_dps": 0.0,
        "front_mm": 200,
        "sim_truth": {
            "x_mm": 155.0,
            "y_mm": 145.0,
            "yaw_deg": 2.0,
        },
    }

    fusion_payload = extract_fusion_telemetry(message)
    truth_payload = extract_simulation_truth(message)

    assert "sim_truth" not in fusion_payload
    observation_value = PoseObservation.from_mapping(fusion_payload)
    assert not hasattr(observation_value, "sim_truth")
    fusion = PoseFusion(
        config=config(),
        initial_cell=(0, 0),
        initial_heading="N",
    )
    estimate = fusion.update(observation_value)
    before = estimate.to_dict()
    evaluation = evaluate_pose(
        estimate,
        TruthPose.from_mapping(truth_payload),
    )
    assert estimate.to_dict() == before
    assert evaluation.position_error_mm == round(
        math.hypot(estimate.x_mm - 155.0, estimate.y_mm - 145.0),
        3,
    )
    assert evaluation.yaw_error_deg >= 0.0


def test_webots_telemetry_separates_deterministic_imu_and_truth_channels():
    engine = MazeSimEngine()

    ready = engine.ready_message()
    telemetry = engine.telemetry_message()

    assert ready["imu_available"] is True
    assert "imu_simulated" in ready["features"]
    assert telemetry["imu_available"] is True
    assert telemetry["imu_quality"] == "simulated"
    assert isinstance(telemetry["imu_yaw_deg"], float)
    assert isinstance(telemetry["yaw_rate_dps"], float)
    assert telemetry["sim_truth"]["cell"] == list(engine.cell)
    assert "sim_truth" not in extract_fusion_telemetry(telemetry)


def test_webots_continuous_wall_evidence_does_not_bias_fused_pose():
    engine = MazeSimEngine()
    params = ParamManager(
        params_path=PARAMS_PATH,
        limits_path=LIMITS_PATH,
    )
    maze = MazeMap.from_definition(
        engine.map_definition,
        wall_threshold_mm=150,
    )
    state = DashboardState(params=params, maze=maze)

    state.update_telemetry(engine.telemetry_message())

    telemetry = state.snapshot()["telemetry"]
    assert telemetry["fusion_left_mm"] > telemetry["left_mm"]
    assert telemetry["fusion_right_mm"] > telemetry["right_mm"]
    assert telemetry["truth_error_cm"] < 1.0

    state.update_telemetry(
        {
            "type": "telemetry",
            "ts_ms": 100,
            "enc_left": 0,
            "enc_right": 0,
            "front_mm": 300,
            "left_mm": 200,
            "right_mm": 200,
            "imu_available": False,
        }
    )
    real_telemetry = state.snapshot()["telemetry"]
    assert "sim_truth" not in real_telemetry
    assert "fusion_front_mm" not in real_telemetry
    assert "truth_error_cm" not in real_telemetry


def test_webots_wall_motion_provides_low_slip_evidence_while_moving():
    engine = MazeSimEngine()
    params = ParamManager(
        params_path=PARAMS_PATH,
        limits_path=LIMITS_PATH,
    )
    maze = MazeMap.from_definition(
        engine.map_definition,
        wall_threshold_mm=150,
    )
    state = DashboardState(params=params, maze=maze)
    state.update_telemetry(engine.telemetry_message())
    engine.handle(
        {
            "type": "action",
            "seq": 1,
            "action_id": "pose-1",
            "name": "move_cell",
            "target_ticks": 1350,
        },
        now_ms=0,
    )

    state.update_telemetry(engine.tick(now_ms=350)[-1])

    slip = state.snapshot()["slip"]
    assert slip["overall_slip_rate"] < 0.02
    assert slip["friction_profile"] == "normal"
    assert slip["is_physical_truth"] is False


def test_no_imu_task_confidence_requires_success_stable_heading_and_two_axes():
    fusion = PoseFusion(
        config=config(),
        initial_cell=(0, 0),
        initial_heading="N",
    )
    estimate = fusion.update(
        observation(ts_ms=0, enc_left=0, enc_right=0),
        wall_constraints=(
            WallConstraint(
                direction="N",
                wall_coordinate_mm=0,
                distance_mm=150,
            ),
            WallConstraint(
                direction="W",
                wall_coordinate_mm=0,
                distance_mm=150,
            ),
        ),
    )

    degraded = fusion.qualify_task_estimate(
        estimate,
        action_success=True,
        stable_grid_heading=True,
        independent_wall_constraints=1,
    )
    qualified = fusion.qualify_task_estimate(
        estimate,
        action_success=True,
        stable_grid_heading=True,
        independent_wall_constraints=2,
    )

    assert degraded.confidence < 0.80
    assert "task_pose_degraded" in degraded.quality_flags
    assert qualified.confidence >= 0.80
    assert "no_imu_wall_qualified" in qualified.quality_flags


def test_dashboard_state_exposes_pose_covariance_quality_and_truth_error():
    params = ParamManager(
        params_path=PARAMS_PATH,
        limits_path=LIMITS_PATH,
    )
    state = DashboardState(params=params, clock_ms=lambda: 1000)

    state.update_telemetry(
        {
            "type": "telemetry",
            "ts_ms": 0,
            "state": "IDLE",
            "enc_left": 0,
            "enc_right": 0,
            "front_mm": 120,
            "left_mm": 120,
            "right_mm": 120,
            "imu_available": True,
            "imu_yaw_deg": 0.0,
            "yaw_rate_dps": 0.0,
            "sim_truth": {
                "x_mm": 125.0,
                "y_mm": 125.0,
                "yaw_deg": 0.0,
            },
        }
    )

    snapshot = state.snapshot()
    telemetry = snapshot["telemetry"]
    assert snapshot["pose"]["grid_cell"] == [0, 0]
    assert len(snapshot["pose"]["covariance"]) == 3
    assert telemetry["pose_confidence"] > 0.0
    assert telemetry["pose_covariance"] == snapshot["pose"]["covariance"]
    assert telemetry["imu_available"] is True
    assert telemetry["truth_error_cm"] >= 0.0


def test_dashboard_preserves_valid_zero_device_timestamp():
    params = ParamManager(
        params_path=PARAMS_PATH,
        limits_path=LIMITS_PATH,
    )
    state = DashboardState(params=params, clock_ms=lambda: 9000)
    state.update_telemetry(
        {
            "type": "telemetry",
            "ts_ms": 0,
            "enc_left": 0,
            "enc_right": 0,
        }
    )
    state.update_telemetry(
        {
            "type": "telemetry",
            "ts_ms": 1000,
            "enc_left": 1350,
            "enc_right": 1350,
        }
    )

    assert state.snapshot()["pose"]["speed_mm_s"] == 250.0
