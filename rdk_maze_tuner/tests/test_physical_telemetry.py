from dataclasses import fields

from rdk_maze_tuner.core.pose_fusion import PoseFusion
from rdk_maze_tuner.core.pose_types import (
    PoseFusionConfig,
    PoseObservation,
)
from rdk_maze_tuner.core.protocol import (
    extract_fusion_telemetry,
    extract_simulation_truth,
)
from rdk_maze_tuner.core.slip_estimator import (
    SlipEstimator,
    SlipEstimatorConfig,
)
from simulation.webots.maze_car.controllers.maze_physical_controller.physical_telemetry import (
    PhysicalTelemetryProvider,
)
from simulation.webots.maze_car.controllers.maze_physical_controller.physical_types import (
    PhysicalDeviceSample,
)
from simulation.webots.maze_car.controllers.maze_physical_controller.truth_observer import (
    SIMULATION_TRUTH_FIELDS,
    TruthObserver,
)
from simulation.webots.maze_car.physical_config import (
    PhysicalProfileRepository,
)


class FakeSupervisorNode:
    def getPosition(self):
        return (1.0, 0.04, -2.0)

    def getOrientation(self):
        return (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)

    def getVelocity(self):
        return (3.0, 0.0, 4.0, 0.0, 2.0, 0.0)


def device_sample() -> PhysicalDeviceSample:
    return PhysicalDeviceSample(
        timestamp_ms=80,
        wheel_angle_left_rad=1.0,
        wheel_angle_right_rad=1.1,
        wheel_speed_left_rad_s=2.0,
        wheel_speed_right_rad_s=2.2,
        enc_left=176,
        enc_right=193,
        raw_front_mm=320.0,
        raw_left_mm=210.0,
        raw_right_mm=205.0,
        front_mm=318.0,
        left_mm=211.0,
        right_mm=206.0,
        imu_available=True,
        imu_yaw_deg=1.2,
        yaw_rate_dps=0.4,
        accel_forward_mps2=0.03,
        quality_flags=("tof_filtered",),
        controller_period_ms=8,
        friction_profile="normal",
    )


def test_device_sample_contains_no_supervisor_or_truth_fields():
    names = {field.name for field in fields(PhysicalDeviceSample)}

    assert "sim_truth" not in names
    assert "position" not in names
    assert "orientation" not in names
    assert "supervisor" not in names


def test_physical_telemetry_provider_consumes_only_device_sample():
    profile = PhysicalProfileRepository().get("normal-v1")
    telemetry = PhysicalTelemetryProvider(
        profile_id=profile.profile_id,
        profile_digest=profile.digest,
    ).build(device_sample())

    assert telemetry["type"] == "telemetry"
    assert telemetry["simulation_backend"] == "physical"
    assert telemetry["sensor_source"] == "webots_device"
    assert telemetry["wheel_angle_left_rad"] == 1.0
    assert telemetry["enc_right"] == 193
    assert telemetry["raw_front_mm"] == 320.0
    assert telemetry["front_mm"] == 318.0
    assert telemetry["imu_yaw_deg"] == 1.2
    assert telemetry["controller_period_ms"] == 8
    assert telemetry["friction_profile"] == "normal"
    assert telemetry["physical_profile_id"] == "normal-v1"
    assert telemetry["physical_profile_digest"] == profile.digest
    assert "sim_truth" not in telemetry


def test_truth_observer_has_an_evaluation_only_allowlist():
    truth = TruthObserver(FakeSupervisorNode()).observe(
        wheel_linear_left_mps=6.0,
        wheel_linear_right_mps=5.0,
        active_surface="normal",
        collision_count=3,
    )

    assert set(truth) == SIMULATION_TRUTH_FIELDS
    assert truth["x_mm"] == 1000.0
    assert truth["y_mm"] == -2000.0
    assert truth["yaw_deg"] == 0.0
    assert truth["linear_speed_mm_s"] == 5000.0
    assert truth["angular_velocity_dps"] > 114.0
    assert truth["left_slip_rate"] > 0.16
    assert truth["right_slip_rate"] == 0.0
    assert truth["active_surface"] == "normal"
    assert truth["collision_count"] == 3


def test_protocol_drops_whole_truth_from_fusion_and_filters_evaluation_fields():
    message = {
        "type": "telemetry",
        "ts_ms": 100,
        "enc_left": 20,
        "enc_right": 21,
        "sim_truth": {
            "x_mm": 1e12,
            "y_mm": -1e12,
            "yaw_deg": 179.0,
            "linear_speed_mm_s": 999999.0,
            "angular_velocity_dps": -999999.0,
            "left_slip_rate": 1.0,
            "right_slip_rate": 1.0,
            "active_surface": "low",
            "collision_count": 4,
            "cell": [99, 99],
            "heading": "S",
            "unsafe_feedback": 123,
        },
    }

    fusion = extract_fusion_telemetry(message)
    truth = extract_simulation_truth(message)

    assert "sim_truth" not in fusion
    assert set(truth) == SIMULATION_TRUTH_FIELDS
    assert "cell" not in truth
    assert "heading" not in truth
    assert "unsafe_feedback" not in truth


def _run_estimators(message):
    evidence = extract_fusion_telemetry(message)
    observation = PoseObservation.from_mapping(evidence)
    fusion = PoseFusion(
        config=PoseFusionConfig(
            cell_width_mm=300.0,
            cell_height_mm=300.0,
            cell_ticks=1350.0,
            turn_90_ticks=720.0,
            wheel_base_mm=135.0,
        )
    )
    pose = fusion.update(observation).to_dict()
    slip = SlipEstimator(
        SlipEstimatorConfig(
            mm_per_tick=300.0 / 1350.0,
            wheel_base_mm=135.0,
        )
    )
    slip_value = slip.update(
        timestamp_ms=observation.timestamp_ms,
        enc_left=observation.enc_left,
        enc_right=observation.enc_right,
        imu_available=observation.imu_available,
        imu_yaw_deg=observation.imu_yaw_deg,
        external_distance_mm=None,
    ).to_dict()
    return pose, slip_value


def test_extreme_truth_cannot_change_pose_fusion_or_slip_estimator():
    sensor_message = {
        "type": "telemetry",
        "ts_ms": 100,
        "enc_left": 20,
        "enc_right": 21,
        "imu_available": True,
        "imu_yaw_deg": 1.0,
        "yaw_rate_dps": 0.1,
    }
    poisoned = {
        **sensor_message,
        "sim_truth": {
            "x_mm": 1e12,
            "y_mm": -1e12,
            "yaw_deg": 179.0,
            "linear_speed_mm_s": 1e9,
            "angular_velocity_dps": -1e9,
            "left_slip_rate": 1.0,
            "right_slip_rate": 1.0,
            "active_surface": "low",
            "collision_count": 999999,
        },
    }

    assert _run_estimators(sensor_message) == _run_estimators(poisoned)
