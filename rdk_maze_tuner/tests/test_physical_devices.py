import math
from dataclasses import replace

import pytest

from simulation.webots.maze_car.physical_config import (
    PhysicalProfileRepository,
)
from simulation.webots.maze_car.controllers.maze_physical_controller.physical_devices import (
    DEVICE_NAMES,
    PhysicalDeviceAdapter,
)
from simulation.webots.maze_car.controllers.maze_physical_controller.physical_types import (
    PhysicalDeviceError,
)


class FakeMotor:
    def __init__(self) -> None:
        self.positions: list[float] = []
        self.velocities: list[float] = []
        self.torques: list[float] = []

    def setPosition(self, value: float) -> None:
        self.positions.append(value)

    def setVelocity(self, value: float) -> None:
        self.velocities.append(value)

    def setAvailableTorque(self, value: float) -> None:
        self.torques.append(value)


class FakeScalarSensor:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value
        self.periods: list[int] = []

    def enable(self, period_ms: int) -> None:
        self.periods.append(period_ms)

    def getValue(self) -> float:
        return self.value


class FakeInertialUnit:
    def __init__(self, values=(0.0, 0.0, 0.0)) -> None:
        self.values = values
        self.periods: list[int] = []

    def enable(self, period_ms: int) -> None:
        self.periods.append(period_ms)

    def getRollPitchYaw(self):
        return self.values


class FakeVectorSensor:
    def __init__(self, values=(0.0, 0.0, 0.0)) -> None:
        self.values = values
        self.periods: list[int] = []

    def enable(self, period_ms: int) -> None:
        self.periods.append(period_ms)

    def getValues(self):
        return self.values


class FakeRobot:
    def __init__(self, devices: dict[str, object]) -> None:
        self.devices = devices
        self.requested_names: list[str] = []

    def getDevice(self, name: str):
        self.requested_names.append(name)
        return self.devices.get(name)


def profile(**tof_overrides):
    value = PhysicalProfileRepository().get("normal-v1")
    if tof_overrides:
        value = replace(value, tof=replace(value.tof, **tof_overrides))
    return value


def devices() -> dict[str, object]:
    return {
        "left wheel motor": FakeMotor(),
        "right wheel motor": FakeMotor(),
        "left wheel encoder": FakeScalarSensor(),
        "right wheel encoder": FakeScalarSensor(),
        "tof front": FakeScalarSensor(0.25),
        "tof left": FakeScalarSensor(0.4),
        "tof right": FakeScalarSensor(0.6),
        "imu": FakeInertialUnit(),
        "gyro": FakeVectorSensor(),
        "accelerometer": FakeVectorSensor(),
    }


def test_adapter_uses_exact_proto_names_and_enables_every_sensor():
    fake_devices = devices()
    robot = FakeRobot(fake_devices)

    PhysicalDeviceAdapter(robot, profile())

    assert tuple(robot.requested_names) == DEVICE_NAMES
    for name in DEVICE_NAMES:
        device = fake_devices[name]
        if hasattr(device, "periods"):
            assert device.periods == [8]


def test_profile_reset_rebinds_devices_after_proto_regeneration():
    fake_devices = devices()
    robot = FakeRobot(fake_devices)
    adapter = PhysicalDeviceAdapter(robot, profile())
    robot.requested_names.clear()

    adapter.apply_profile(
        PhysicalProfileRepository().get("low-v1")
    )

    assert tuple(robot.requested_names) == DEVICE_NAMES
    assert adapter.profile.profile_id == "low-v1"


def test_missing_core_device_stops_both_motors_and_raises_named_error():
    fake_devices = devices()
    fake_devices.pop("tof right")
    left = fake_devices["left wheel motor"]
    right = fake_devices["right wheel motor"]

    with pytest.raises(PhysicalDeviceError) as captured:
        PhysicalDeviceAdapter(FakeRobot(fake_devices), profile())

    assert captured.value.code == "SIM_DEVICE_MISSING"
    assert captured.value.details["device"] == "tof right"
    assert left.velocities[-1] == 0.0
    assert right.velocities[-1] == 0.0


def test_wheel_angles_are_quantized_to_signed_ticks_and_speed_uses_eight_ms():
    fake_devices = devices()
    adapter = PhysicalDeviceAdapter(FakeRobot(fake_devices), profile())

    baseline = adapter.sample(timestamp_ms=0)
    fake_devices["left wheel encoder"].value = 2.0 * math.pi
    fake_devices["right wheel encoder"].value = -2.0 * math.pi
    sample = adapter.sample(timestamp_ms=8)

    assert baseline.enc_left == baseline.enc_right == 0
    assert sample.enc_left == 1103
    assert sample.enc_right == -1103
    assert sample.wheel_speed_left_rad_s == pytest.approx(2.0 * math.pi / 0.008)
    assert sample.wheel_speed_right_rad_s == pytest.approx(-2.0 * math.pi / 0.008)


def test_tof_converts_to_mm_clamps_filters_and_marks_quality():
    fake_devices = devices()
    fake_devices["tof front"].value = 0.01
    fake_devices["tof left"].value = 5.0
    fake_devices["tof right"].value = 0.4
    adapter = PhysicalDeviceAdapter(
        FakeRobot(fake_devices),
        profile(noise_std_mm=0.0, dropout_rate=0.0),
    )

    first = adapter.sample(timestamp_ms=0)
    fake_devices["tof right"].value = 0.8
    second = adapter.sample(timestamp_ms=8)

    assert first.raw_front_mm == 10.0
    assert first.front_mm == 30.0
    assert first.left_mm == 2000.0
    assert first.right_mm == 400.0
    assert "tof_front_clamped_low" in first.quality_flags
    assert "tof_left_clamped_high" in first.quality_flags
    assert 400.0 < second.right_mm < 800.0


def test_tof_noise_and_dropout_are_repeatable_with_fixed_seed():
    deterministic_profile = profile(noise_std_mm=4.0, dropout_rate=0.5)
    left_adapter = PhysicalDeviceAdapter(FakeRobot(devices()), deterministic_profile)
    right_adapter = PhysicalDeviceAdapter(FakeRobot(devices()), deterministic_profile)

    left_samples = [
        left_adapter.sample(timestamp_ms=index * 8)
        for index in range(12)
    ]
    right_samples = [
        right_adapter.sample(timestamp_ms=index * 8)
        for index in range(12)
    ]

    assert left_samples == right_samples
    assert any(
        "tof_front_dropout" in sample.quality_flags
        for sample in left_samples
    )
    assert "tof_noise_enabled" in left_samples[0].quality_flags
    assert "tof_dropout_enabled" in left_samples[0].quality_flags


def test_ideal_sensor_mode_disables_adapter_noise_and_dropout():
    fake_devices = devices()
    adapter = PhysicalDeviceAdapter(
        FakeRobot(fake_devices),
        profile(noise_std_mm=40.0, dropout_rate=1.0),
        sensor_noise_enabled=False,
        sensor_dropout_enabled=False,
    )

    first = adapter.sample(timestamp_ms=0)
    second = adapter.sample(timestamp_ms=8)

    assert first.front_mm == 250.0
    assert second.front_mm == 250.0
    assert "tof_noise_enabled" not in first.quality_flags
    assert "tof_dropout_enabled" not in first.quality_flags
    assert not any(
        flag.endswith("_dropout")
        for flag in first.quality_flags
    )


def test_non_finite_imu_stops_motors_and_raises_physics_error():
    fake_devices = devices()
    fake_devices["imu"].values = (0.0, 0.0, float("nan"))
    adapter = PhysicalDeviceAdapter(FakeRobot(fake_devices), profile())

    with pytest.raises(PhysicalDeviceError) as captured:
        adapter.sample(timestamp_ms=8)

    assert captured.value.code == "SIM_PHYSICS_ERROR"
    assert fake_devices["left wheel motor"].velocities[-1] == 0.0
    assert fake_devices["right wheel motor"].velocities[-1] == 0.0


def test_imu_heading_uses_navigation_clockwise_sign_convention():
    fake_devices = devices()
    fake_devices["imu"].values = (
        0.0,
        0.0,
        math.radians(30.0),
    )
    fake_devices["gyro"].values = (
        0.0,
        math.radians(10.0),
        0.0,
    )
    configured_profile = profile()
    configured_profile = replace(
        configured_profile,
        imu=replace(
            configured_profile.imu,
            yaw_noise_std_deg=0.0,
            gyro_noise_std_dps=0.0,
        ),
    )
    adapter = PhysicalDeviceAdapter(
        FakeRobot(fake_devices),
        configured_profile,
    )

    sample = adapter.sample(timestamp_ms=8)

    assert sample.imu_yaw_deg == pytest.approx(330.0)
    assert sample.yaw_rate_dps == pytest.approx(-10.0)


def test_motor_commands_are_finite_and_clamped_to_profile_limits():
    fake_devices = devices()
    configured_profile = profile()
    adapter = PhysicalDeviceAdapter(FakeRobot(fake_devices), configured_profile)

    applied = adapter.command_wheels(
        left_velocity_rad_s=100.0,
        right_velocity_rad_s=-100.0,
        torque_nm=1.0,
    )

    assert applied.left_velocity_rad_s == configured_profile.motor.max_velocity_rad_s
    assert applied.right_velocity_rad_s == -configured_profile.motor.max_velocity_rad_s
    assert applied.torque_nm == configured_profile.motor.max_torque_nm
    assert applied.limited is True

    with pytest.raises(PhysicalDeviceError) as captured:
        adapter.command_wheels(
            left_velocity_rad_s=float("inf"),
            right_velocity_rad_s=0.0,
        )
    assert captured.value.code == "SIM_INVALID_MOTOR_COMMAND"
    assert fake_devices["left wheel motor"].velocities[-1] == 0.0
    assert fake_devices["right wheel motor"].velocities[-1] == 0.0
