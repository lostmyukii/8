from dataclasses import replace

import pytest

from simulation.webots.maze_car.controllers.maze_physical_controller.motor_model import (
    DualMotorModel,
)
from simulation.webots.maze_car.controllers.maze_physical_controller.pid import (
    ControlInputError,
    PidConfig,
    VelocityPid,
)
from simulation.webots.maze_car.physical_config import (
    PhysicalProfileRepository,
)


def test_velocity_pid_uses_eight_ms_and_derivative_on_measurement():
    pid = VelocityPid(
        PidConfig(
            kp=0.4,
            ki=0.2,
            kd=0.1,
            integral_limit=2.0,
            output_limit=10.0,
        )
    )

    first = pid.update(setpoint=1.0, measurement=0.0, dt_s=0.008)
    setpoint_step = pid.update(
        setpoint=2.0,
        measurement=0.0,
        dt_s=0.008,
    )
    measurement_step = pid.update(
        setpoint=2.0,
        measurement=0.5,
        dt_s=0.008,
    )

    assert first.derivative == 0.0
    assert setpoint_step.derivative == 0.0
    assert measurement_step.derivative < 0.0


def test_pid_clamps_integral_and_output_and_reset_clears_history():
    pid = VelocityPid(
        PidConfig(
            kp=10.0,
            ki=4.0,
            kd=1.0,
            integral_limit=0.25,
            output_limit=0.8,
        )
    )

    outputs = [
        pid.update(setpoint=10.0, measurement=0.0, dt_s=0.008)
        for _ in range(20)
    ]

    assert all(abs(item.output) <= 0.8 for item in outputs)
    assert abs(outputs[-1].integral) <= 0.25
    pid.reset()
    after_reset = pid.update(
        setpoint=0.0,
        measurement=4.0,
        dt_s=0.008,
    )
    assert after_reset.derivative == 0.0


def test_pid_non_finite_input_fails_closed_and_zeros_last_output():
    pid = VelocityPid(
        PidConfig(
            kp=1.0,
            ki=0.0,
            kd=0.0,
            integral_limit=1.0,
            output_limit=1.0,
        )
    )
    pid.update(setpoint=1.0, measurement=0.0, dt_s=0.008)

    with pytest.raises(ControlInputError):
        pid.update(
            setpoint=float("nan"),
            measurement=0.0,
            dt_s=0.008,
        )

    assert pid.last_output == 0.0


def test_motor_model_applies_dead_zone_gain_response_and_torque_limit():
    profile = PhysicalProfileRepository().get("normal-v1")
    asymmetric = replace(
        profile,
        motor=replace(
            profile.motor,
            left_gain=0.8,
            right_gain=1.0,
        ),
    )
    model = DualMotorModel(asymmetric.motor)

    dead = model.step(pwm_left=0.1, pwm_right=-0.1, dt_s=0.008)
    first = model.step(pwm_left=1.0, pwm_right=1.0, dt_s=0.008)
    second = model.step(pwm_left=1.0, pwm_right=1.0, dt_s=0.008)

    assert dead.left_velocity_rad_s == 0.0
    assert dead.right_velocity_rad_s == 0.0
    assert 0.0 < first.left_velocity_rad_s < first.right_velocity_rad_s
    assert first.right_velocity_rad_s < profile.motor.max_velocity_rad_s
    assert second.right_velocity_rad_s > first.right_velocity_rad_s
    assert first.left_torque_nm <= profile.motor.max_torque_nm
    assert first.right_torque_nm <= profile.motor.max_torque_nm


def test_motor_model_is_deterministic_and_non_finite_input_resets_to_zero():
    motor = PhysicalProfileRepository().get("normal-v1").motor
    left = DualMotorModel(motor)
    right = DualMotorModel(motor)
    sequence = [(0.4, 0.5), (0.7, -0.2), (0.0, 0.0)]

    assert [
        left.step(pwm_left=a, pwm_right=b, dt_s=0.008)
        for a, b in sequence
    ] == [
        right.step(pwm_left=a, pwm_right=b, dt_s=0.008)
        for a, b in sequence
    ]

    with pytest.raises(ControlInputError):
        left.step(
            pwm_left=float("inf"),
            pwm_right=0.0,
            dt_s=0.008,
        )
    assert left.current_velocities == (0.0, 0.0)
