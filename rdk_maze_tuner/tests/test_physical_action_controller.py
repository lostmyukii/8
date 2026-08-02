from dataclasses import replace

import pytest

from simulation.webots.maze_car.controllers.maze_physical_controller.action_controller import (
    ActionControlConfig,
    ActionRejected,
    ActionRequest,
    PhysicalActionController,
)
from simulation.webots.maze_car.controllers.maze_physical_controller.physical_types import (
    PhysicalDeviceSample,
)
from simulation.webots.maze_car.physical_config import (
    PhysicalProfileRepository,
)


def sample(
    *,
    ts_ms=0,
    enc_left=0,
    enc_right=0,
    left_speed=0.0,
    right_speed=0.0,
    front_mm=500.0,
    left_mm=300.0,
    right_mm=300.0,
    yaw_deg=0.0,
    yaw_rate=0.0,
    accel=0.0,
):
    return PhysicalDeviceSample(
        timestamp_ms=ts_ms,
        wheel_angle_left_rad=0.0,
        wheel_angle_right_rad=0.0,
        wheel_speed_left_rad_s=left_speed,
        wheel_speed_right_rad_s=right_speed,
        enc_left=enc_left,
        enc_right=enc_right,
        raw_front_mm=front_mm,
        raw_left_mm=left_mm,
        raw_right_mm=right_mm,
        front_mm=front_mm,
        left_mm=left_mm,
        right_mm=right_mm,
        imu_available=True,
        imu_yaw_deg=yaw_deg,
        yaw_rate_dps=yaw_rate,
        accel_forward_mps2=accel,
        quality_flags=(),
        controller_period_ms=8,
        friction_profile="normal",
    )


def config(**overrides):
    base = ActionControlConfig(
        default_target_ticks=999,
        position_tolerance_ticks=3,
        angle_tolerance_deg=4.0,
        settle_speed_rad_s=0.3,
        settle_ticks_required=3,
        slowdown_ticks=30,
        minimum_speed_scale=0.2,
        danger_stop_mm=60.0,
        action_timeout_ms=1000,
        heartbeat_timeout_ms=500,
        stall_timeout_ms=200,
        wheelspin_timeout_ms=200,
        wheelspin_predicted_mm=10.0,
        wheelspin_external_mm=2.0,
        collision_accel_mps2=6.0,
        collision_distance_mm=100.0,
    )
    return replace(base, **overrides)


def controller(**overrides):
    profile = PhysicalProfileRepository().get("normal-v1")
    return PhysicalActionController(
        profile=profile,
        config=config(**overrides),
    )


def test_move_action_is_non_blocking_and_requires_consecutive_settling_ticks():
    control = controller()
    initial = sample(ts_ms=0)
    control.start(
        ActionRequest(
            action_id="move-1",
            name="move_cell",
            target_ticks=100,
            speed=0.5,
        ),
        sample=initial,
        now_ms=0,
    )

    moving = control.tick(sample=sample(ts_ms=8), now_ms=8)
    settling = control.tick(
        sample=sample(
            ts_ms=16,
            enc_left=98,
            enc_right=98,
            left_speed=0.1,
            right_speed=0.1,
        ),
        now_ms=16,
    )
    stable_1 = control.tick(
        sample=sample(ts_ms=24, enc_left=100, enc_right=100),
        now_ms=24,
    )
    stable_2 = control.tick(
        sample=sample(ts_ms=32, enc_left=100, enc_right=100),
        now_ms=32,
    )
    finished = control.tick(
        sample=sample(ts_ms=40, enc_left=100, enc_right=100),
        now_ms=40,
    )

    assert moving.state == "MOVING_CELL"
    assert moving.event is None
    assert settling.state == "SETTLING"
    assert stable_1.event is None
    assert stable_2.event is None
    assert finished.state == "IDLE"
    assert finished.event["type"] == "done"
    assert finished.event["action_id"] == "move-1"
    assert finished.event["target_ticks"] == 100


def test_straight_control_uses_encoder_difference_and_imu_heading():
    control = controller()
    control.start(
        ActionRequest("straight", "move_cell", 200, 0.6),
        sample=sample(ts_ms=0, yaw_deg=0.0),
        now_ms=0,
    )

    output = control.tick(
        sample=sample(
            ts_ms=8,
            enc_left=20,
            enc_right=10,
            yaw_deg=5.0,
        ),
        now_ms=8,
    )

    assert output.target_velocity_left_rad_s < output.target_velocity_right_rad_s
    assert output.motor_available_torque_nm == pytest.approx(0.12)
    assert output.telemetry["encoder_balance_error_ticks"] == 10
    assert output.telemetry["heading_error_deg"] < 0


def test_velocity_control_keeps_dead_zone_feedforward_at_setpoint():
    control = controller()
    control.start(
        ActionRequest("feedforward", "move_cell", 200, 0.5),
        sample=sample(ts_ms=0),
        now_ms=0,
    )
    control.tick(sample=sample(ts_ms=8), now_ms=8)

    output = control.tick(
        sample=sample(
            ts_ms=16,
            enc_left=20,
            enc_right=20,
            left_speed=10.0,
            right_speed=10.0,
        ),
        now_ms=16,
    )

    assert output.pwm_left > 0.18
    assert output.pwm_right > 0.18


@pytest.mark.parametrize(
    ("name", "left_sign", "right_sign"),
    [
        ("turn_left", -1, 1),
        ("turn_right", 1, -1),
        ("turn_back", 1, -1),
    ],
)
def test_turns_command_opposite_wheel_velocities(name, left_sign, right_sign):
    control = controller()
    control.start(
        ActionRequest("turn-1", name, 100, 0.5),
        sample=sample(ts_ms=0, yaw_deg=0.0),
        now_ms=0,
    )

    output = control.tick(sample=sample(ts_ms=8), now_ms=8)

    assert output.target_velocity_left_rad_s * left_sign > 0
    assert output.target_velocity_right_rad_s * right_sign > 0
    assert output.motor_available_torque_nm == pytest.approx(0.60)
    assert output.event is None


def test_turn_uses_heading_for_slowdown_after_encoder_target_is_reached():
    control = controller()
    control.start(
        ActionRequest("turn-heading", "turn_left", 100, 0.5),
        sample=sample(ts_ms=0, yaw_deg=0.0),
        now_ms=0,
    )

    output = control.tick(
        sample=sample(
            ts_ms=8,
            enc_left=-100,
            enc_right=100,
            yaw_deg=330.0,
        ),
        now_ms=8,
    )

    assert output.state == "TURNING_LEFT"
    assert abs(output.target_velocity_left_rad_s) > 2.0
    assert abs(output.target_velocity_right_rad_s) > 2.0


def test_turn_can_settle_from_heading_after_minimum_encoder_progress():
    control = controller()
    control.start(
        ActionRequest("turn-settle", "turn_left", 100, 0.5),
        sample=sample(ts_ms=0, yaw_deg=0.0),
        now_ms=0,
    )

    settling = control.tick(
        sample=sample(
            ts_ms=8,
            enc_left=-60,
            enc_right=60,
            yaw_deg=270.0,
        ),
        now_ms=8,
    )

    assert settling.state == "SETTLING"


def test_turn_crossing_is_latched_when_a_control_step_passes_the_target():
    control = controller(angle_tolerance_deg=1.0)
    control.start(
        ActionRequest("turn-crossing", "turn_left", 100, 0.5),
        sample=sample(ts_ms=0, yaw_deg=0.0),
        now_ms=0,
    )
    before = control.tick(
        sample=sample(
            ts_ms=8,
            enc_left=-80,
            enc_right=80,
            yaw_deg=272.0,
        ),
        now_ms=8,
    )
    crossed = control.tick(
        sample=sample(
            ts_ms=16,
            enc_left=-90,
            enc_right=90,
            yaw_deg=268.0,
        ),
        now_ms=16,
    )

    assert before.state == "TURNING_LEFT"
    assert crossed.state == "SETTLING"
    assert crossed.event is None
    assert crossed.motor_available_torque_nm == pytest.approx(0.60)


def test_turn_predictively_brakes_from_imu_yaw_rate():
    control = controller(angle_tolerance_deg=1.0)
    control.start(
        ActionRequest("turn-brake", "turn_right", 100, 0.5),
        sample=sample(ts_ms=0, yaw_deg=0.0),
        now_ms=0,
    )

    braking = control.tick(
        sample=sample(
            ts_ms=8,
            enc_left=80,
            enc_right=-80,
            yaw_deg=80.0,
            yaw_rate=300.0,
        ),
        now_ms=8,
    )

    assert braking.state == "SETTLING"
    assert braking.motor_available_torque_nm == pytest.approx(0.60)


def test_turn_resumes_low_speed_correction_after_predictive_braking_coasts():
    control = controller(angle_tolerance_deg=1.0)
    control.start(
        ActionRequest("turn-correct", "turn_left", 100, 0.5),
        sample=sample(ts_ms=0, yaw_deg=0.0),
        now_ms=0,
    )
    control.tick(
        sample=sample(
            ts_ms=8,
            enc_left=-80,
            enc_right=80,
            yaw_deg=275.0,
            yaw_rate=-300.0,
        ),
        now_ms=8,
    )

    correction = control.tick(
        sample=sample(
            ts_ms=16,
            enc_left=-90,
            enc_right=90,
            yaw_deg=265.0,
            left_speed=0.0,
            right_speed=0.0,
        ),
        now_ms=16,
    )

    assert correction.state == "TURNING_LEFT"
    assert correction.event is None


def test_settling_does_not_report_motor_stall_after_wheels_are_stopped():
    control = controller(
        stall_timeout_ms=20,
        heartbeat_timeout_ms=5000,
        action_timeout_ms=5000,
    )
    control.start(
        ActionRequest("settle-no-stall", "turn_left", 100, 0.5),
        sample=sample(ts_ms=0, yaw_deg=0.0),
        now_ms=0,
    )
    entered = control.tick(
        sample=sample(
            ts_ms=8,
            enc_left=-100,
            enc_right=100,
            yaw_deg=270.0,
        ),
        now_ms=8,
    )
    stopped = control.tick(
        sample=sample(
            ts_ms=100,
            enc_left=-100,
            enc_right=100,
            yaw_deg=270.0,
        ),
        now_ms=100,
    )

    assert entered.state == "SETTLING"
    assert stopped.state == "SETTLING"
    assert stopped.event is None


def test_zero_turn_target_derives_ticks_from_profile_geometry():
    control = controller()
    control.start(
        ActionRequest("turn-derived", "turn_left", 0, 0.5),
        sample=sample(ts_ms=0, yaw_deg=0.0),
        now_ms=0,
    )
    expected = round(0.135 / (8.0 * 0.0325) * 1103)
    ticks = int(expected * 0.6)
    control.tick(
        sample=sample(
            ts_ms=8,
            enc_left=-ticks,
            enc_right=ticks,
            yaw_deg=270.0,
        ),
        now_ms=8,
    )
    control.tick(
        sample=sample(
            ts_ms=16,
            enc_left=-ticks,
            enc_right=ticks,
            yaw_deg=270.0,
        ),
        now_ms=16,
    )
    control.tick(
        sample=sample(
            ts_ms=24,
            enc_left=-ticks,
            enc_right=ticks,
            yaw_deg=270.0,
        ),
        now_ms=24,
    )
    finished = control.tick(
        sample=sample(
            ts_ms=32,
            enc_left=-ticks,
            enc_right=ticks,
            yaw_deg=270.0,
        ),
        now_ms=32,
    )

    assert finished.event["type"] == "done"
    assert finished.event["target_ticks"] == expected


def test_remaining_ticks_drive_slowdown_and_active_action_cannot_be_overwritten():
    control = controller()
    request = ActionRequest("move-2", "move_cell", 100, 0.8)
    control.start(request, sample=sample(ts_ms=0), now_ms=0)
    fast = control.tick(sample=sample(ts_ms=8), now_ms=8)
    slow = control.tick(
        sample=sample(ts_ms=16, enc_left=80, enc_right=80),
        now_ms=16,
    )

    assert abs(slow.target_velocity_left_rad_s) < abs(
        fast.target_velocity_left_rad_s
    )
    with pytest.raises(ActionRejected):
        control.start(
            ActionRequest("move-3", "move_cell", 50, 0.4),
            sample=sample(ts_ms=16),
            now_ms=16,
        )


@pytest.mark.parametrize(
    ("kind", "config_overrides", "tick_sample", "now_ms", "code"),
    [
        (
            "obstacle",
            {},
            sample(ts_ms=8, front_mm=40.0),
            8,
            "OBSTACLE_TOO_CLOSE",
        ),
        (
            "timeout",
            {"action_timeout_ms": 20},
            sample(ts_ms=24),
            24,
            "ACTION_TIMEOUT",
        ),
        (
            "heartbeat",
            {"heartbeat_timeout_ms": 20},
            sample(ts_ms=24),
            24,
            "HEARTBEAT_TIMEOUT",
        ),
        (
            "stall",
            {
                "stall_timeout_ms": 20,
                "heartbeat_timeout_ms": 200,
            },
            sample(ts_ms=24),
            24,
            "MOTOR_STALL",
        ),
        (
            "collision",
            {},
            sample(
                ts_ms=8,
                front_mm=80.0,
                left_speed=0.0,
                right_speed=0.0,
                accel=8.0,
            ),
            8,
            "COLLISION_SUSPECTED",
        ),
    ],
)
def test_safety_failures_keep_action_id(
    kind,
    config_overrides,
    tick_sample,
    now_ms,
    code,
):
    control = controller(**config_overrides)
    control.start(
        ActionRequest("safe-1", "move_cell", 100, 0.5),
        sample=sample(ts_ms=0),
        now_ms=0,
    )

    output = control.tick(sample=tick_sample, now_ms=now_ms)

    assert output.event["type"] == "error"
    assert output.event["code"] == code
    assert output.event["action_id"] == "safe-1"
    assert output.pwm_left == output.pwm_right == 0.0


def test_persistent_wheelspin_uses_encoder_and_external_motion_evidence():
    control = controller(
        wheelspin_timeout_ms=20,
        heartbeat_timeout_ms=200,
        stall_timeout_ms=200,
    )
    control.start(
        ActionRequest("spin-1", "move_cell", 500, 0.8),
        sample=sample(ts_ms=0, front_mm=500.0),
        now_ms=0,
    )

    output = control.tick(
        sample=sample(
            ts_ms=24,
            enc_left=100,
            enc_right=100,
            left_speed=12.0,
            right_speed=12.0,
            front_mm=500.0,
            accel=0.0,
        ),
        now_ms=24,
    )

    assert output.event["code"] == "WHEELSPIN_PERSISTENT"
    assert output.event["action_id"] == "spin-1"


def test_open_space_without_a_range_target_does_not_fake_wheelspin():
    control = controller(
        wheelspin_timeout_ms=20,
        heartbeat_timeout_ms=200,
        stall_timeout_ms=200,
    )
    control.start(
        ActionRequest("open-space", "move_cell", 500, 0.8),
        sample=sample(ts_ms=0, front_mm=2000.0),
        now_ms=0,
    )

    output = control.tick(
        sample=sample(
            ts_ms=24,
            enc_left=100,
            enc_right=100,
            left_speed=12.0,
            right_speed=12.0,
            front_mm=2000.0,
        ),
        now_ms=24,
    )

    assert output.event is None


@pytest.mark.parametrize(("method", "code"), [("pause", "PAUSED"), ("stop", "STOPPED")])
def test_pause_and_stop_decelerate_then_return_non_success_event(method, code):
    control = controller()
    control.start(
        ActionRequest("cancel-1", "move_cell", 300, 0.8),
        sample=sample(ts_ms=0),
        now_ms=0,
    )
    control.tick(
        sample=sample(ts_ms=8, left_speed=6.0, right_speed=6.0),
        now_ms=8,
    )

    getattr(control, method)()
    braking = control.tick(
        sample=sample(ts_ms=16, left_speed=2.0, right_speed=2.0),
        now_ms=16,
    )
    finished = control.tick(
        sample=sample(ts_ms=24, left_speed=0.0, right_speed=0.0),
        now_ms=24,
    )

    assert braking.state in {"PAUSING", "STOPPING"}
    assert braking.event is None
    assert finished.event["type"] == "error"
    assert finished.event["code"] == code
    assert finished.event["action_id"] == "cancel-1"


def test_estop_is_immediate_latched_and_requires_explicit_clear():
    control = controller()
    initial = sample(ts_ms=0)
    control.start(
        ActionRequest("estop-1", "move_cell", 300, 0.8),
        sample=initial,
        now_ms=0,
    )

    stopped = control.estop(now_ms=8)

    assert stopped.state == "ESTOP"
    assert stopped.pwm_left == stopped.pwm_right == 0.0
    assert stopped.event["code"] == "ESTOP"
    with pytest.raises(ActionRejected):
        control.start(
            ActionRequest("blocked", "move_cell", 20, 0.2),
            sample=initial,
            now_ms=8,
        )
    assert control.clear_estop() is True
    control.start(
        ActionRequest("recovered", "move_cell", 20, 0.2),
        sample=initial,
        now_ms=16,
    )
    assert control.state == "MOVING_CELL"


def test_physical_controller_recovery_actions_are_locally_bounded():
    control = controller(
        base_speed_limit=0.25,
        turn_speed_limit=0.18,
        cell_target_ticks=1350,
        turn_90_ticks=720,
    )
    control.start(
        ActionRequest(
            "nudge-1",
            "nudge_forward",
            300,
            0.10,
            recovery=True,
            parent_action_id="move-1",
        ),
        sample=sample(),
        now_ms=0,
    )
    assert control.state == "MOVING_CELL"
    control.reset()

    control.start(
        ActionRequest(
            "align-1",
            "align_heading",
            60,
            0.09,
            recovery=True,
            direction="left",
            parent_action_id="move-1",
        ),
        sample=sample(),
        now_ms=10,
    )
    assert control.state == "TURNING_LEFT"
    control.reset()

    with pytest.raises(ActionRejected, match="direction"):
        control.start(
            ActionRequest(
                "align-bad",
                "align_heading",
                60,
                0.09,
                recovery=True,
                direction="back",
            ),
            sample=sample(),
            now_ms=20,
        )
    assert control.state == "IDLE"

    with pytest.raises(ActionRejected, match="bounded"):
        control.start(
            ActionRequest(
                "nudge-too-far",
                "nudge_forward",
                400,
                0.10,
                recovery=True,
            ),
            sample=sample(),
            now_ms=30,
        )
    assert control.state == "IDLE"
