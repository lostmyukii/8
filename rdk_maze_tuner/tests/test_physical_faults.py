from dataclasses import replace

import pytest

from simulation.webots.maze_car.controllers.maze_physical_controller.action_controller import (
    ActionControlConfig,
    ActionRejected,
    ActionRequest,
    PhysicalActionController,
)
from simulation.webots.maze_car.controllers.maze_physical_controller.physical_devices import (
    PhysicalDeviceAdapter,
)
from simulation.webots.maze_car.controllers.maze_physical_controller.physical_types import (
    PhysicalDeviceError,
    PhysicalDeviceSample,
)
from simulation.webots.maze_car.physical_config import (
    PhysicalConfigError,
    PhysicalProfileRepository,
)


def sample(**overrides):
    values = {
        "timestamp_ms": 0,
        "wheel_angle_left_rad": 0.0,
        "wheel_angle_right_rad": 0.0,
        "wheel_speed_left_rad_s": 0.0,
        "wheel_speed_right_rad_s": 0.0,
        "enc_left": 0,
        "enc_right": 0,
        "raw_front_mm": 500.0,
        "raw_left_mm": 300.0,
        "raw_right_mm": 300.0,
        "front_mm": 500.0,
        "left_mm": 300.0,
        "right_mm": 300.0,
        "imu_available": True,
        "imu_yaw_deg": 0.0,
        "yaw_rate_dps": 0.0,
        "accel_forward_mps2": 0.0,
        "quality_flags": (),
        "controller_period_ms": 8,
        "friction_profile": "normal",
    }
    values.update(overrides)
    return PhysicalDeviceSample(**values)


def controller(**overrides):
    config = replace(
        ActionControlConfig(),
        action_timeout_ms=40,
        heartbeat_timeout_ms=30,
        stall_timeout_ms=20,
        wheelspin_timeout_ms=20,
        wheelspin_predicted_mm=10,
        wheelspin_external_mm=2,
        collision_accel_mps2=6,
        collision_distance_mm=100,
        **overrides,
    )
    return PhysicalActionController(
        profile=PhysicalProfileRepository().get("normal-v1"),
        config=config,
    )


class MissingRobot:
    def getDevice(self, _name):
        return None


def test_missing_device_and_invalid_profile_fail_closed():
    profile = PhysicalProfileRepository().get("normal-v1")

    with pytest.raises(PhysicalDeviceError) as missing:
        PhysicalDeviceAdapter(MissingRobot(), profile)
    assert missing.value.code == "SIM_DEVICE_MISSING"

    with pytest.raises(PhysicalConfigError):
        PhysicalProfileRepository().get("not-a-profile")


@pytest.mark.parametrize(
    ("tick", "now_ms", "code"),
    [
        (sample(timestamp_ms=8, front_mm=40), 8, "OBSTACLE_TOO_CLOSE"),
        (sample(timestamp_ms=48), 48, "ACTION_TIMEOUT"),
        (sample(timestamp_ms=32), 32, "HEARTBEAT_TIMEOUT"),
        (sample(timestamp_ms=24), 24, "MOTOR_STALL"),
        (
            sample(
                timestamp_ms=8,
                front_mm=80,
                accel_forward_mps2=8,
            ),
            8,
            "COLLISION_SUSPECTED",
        ),
        (
            sample(
                timestamp_ms=24,
                enc_left=100,
                enc_right=100,
                wheel_speed_left_rad_s=10,
                wheel_speed_right_rad_s=10,
            ),
            24,
            "WHEELSPIN_PERSISTENT",
        ),
    ],
)
def test_fault_matrix_keeps_action_id_and_zeroes_motors(
    tick,
    now_ms,
    code,
):
    control = controller()
    control.start(
        ActionRequest("fault-action", "move_cell", 500, 0.5),
        sample=sample(),
        now_ms=0,
    )

    output = control.tick(sample=tick, now_ms=now_ms)

    assert output.event["code"] == code
    assert output.event["action_id"] == "fault-action"
    assert output.pwm_left == output.pwm_right == 0


@pytest.mark.parametrize(
    ("method", "code"),
    [("pause", "PAUSED"), ("stop", "STOPPED")],
)
def test_pause_and_stop_return_traceable_non_success(method, code):
    control = controller()
    control.start(
        ActionRequest("cancel-action", "move_cell", 500, 0.5),
        sample=sample(),
        now_ms=0,
    )

    getattr(control, method)()
    output = control.tick(sample=sample(timestamp_ms=8), now_ms=8)

    assert output.event["code"] == code
    assert output.event["action_id"] == "cancel-action"


def test_estop_is_latched_until_explicit_clear():
    control = controller()
    initial = sample()
    control.start(
        ActionRequest("estop-action", "move_cell", 500, 0.5),
        sample=initial,
        now_ms=0,
    )

    stopped = control.estop(now_ms=8)

    assert stopped.event["code"] == "ESTOP"
    with pytest.raises(ActionRejected):
        control.start(
            ActionRequest("blocked", "move_cell", 10, 0.2),
            sample=initial,
            now_ms=8,
        )
    assert control.clear_estop() is True


def test_non_finite_sample_contract_is_invalid():
    invalid = sample(front_mm=float("nan"))

    with pytest.raises(PhysicalDeviceError) as raised:
        invalid.require_finite()

    assert raised.value.code == "SIM_PHYSICS_ERROR"
