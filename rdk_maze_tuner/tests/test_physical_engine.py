from dataclasses import replace

from rdk_maze_tuner.core.maze_validation import validate_map_definition
from simulation.webots.maze_car.controllers.maze_physical_controller.physical_engine import (
    PhysicalMazeEngine,
)
from simulation.webots.maze_car.controllers.maze_physical_controller.physical_types import (
    AppliedMotorCommand,
    PhysicalDeviceSample,
)
from simulation.webots.maze_car.map_loader import (
    compile_map,
    default_map_definition,
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
    yaw_deg=0.0,
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
        raw_left_mm=300.0,
        raw_right_mm=300.0,
        front_mm=front_mm,
        left_mm=300.0,
        right_mm=300.0,
        imu_available=True,
        imu_yaw_deg=yaw_deg,
        yaw_rate_dps=0.0,
        accel_forward_mps2=0.0,
        quality_flags=(),
        controller_period_ms=8,
        friction_profile="normal",
    )


class FakeDeviceAdapter:
    def __init__(self) -> None:
        self.current_sample = sample()
        self.events: list[tuple] = []
        self.profile = None

    def sample(self, *, timestamp_ms: int):
        self.events.append(("sample", timestamp_ms))
        return replace(self.current_sample, timestamp_ms=timestamp_ms)

    def command_wheels(
        self,
        *,
        left_velocity_rad_s: float,
        right_velocity_rad_s: float,
        torque_nm: float | None = None,
    ):
        self.events.append(
            (
                "command",
                left_velocity_rad_s,
                right_velocity_rad_s,
                torque_nm,
            )
        )
        return AppliedMotorCommand(
            left_velocity_rad_s=left_velocity_rad_s,
            right_velocity_rad_s=right_velocity_rad_s,
            torque_nm=float(torque_nm or 0.0),
            limited=False,
        )

    def safe_stop(self) -> None:
        self.events.append(("safe_stop",))

    def reset(self) -> None:
        self.events.append(("reset",))

    def apply_profile(self, profile) -> None:
        self.profile = profile
        self.events.append(("profile", profile.profile_id))
        self.reset()


class FakeWorld:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def apply_profile(self, profile) -> None:
        self.events.append(("profile", profile.profile_id))

    def load_map(self, definition):
        self.events.append(("map", definition.content_digest))
        return compile_map(definition)

    def reset_pose(self, compiled) -> None:
        self.events.append(
            ("pose", compiled.start_cell, compiled.start_heading)
        )

    def refresh_device_samples(self) -> None:
        self.events.append(("refresh_devices",))


class FakeTruthObserver:
    def __init__(self, device: FakeDeviceAdapter) -> None:
        self.device = device
        self.calls = 0

    def observe(self, **_kwargs):
        self.calls += 1
        self.device.events.append(("truth",))
        return {
            "x_mm": 100.0,
            "y_mm": 200.0,
            "yaw_deg": 0.0,
            "linear_speed_mm_s": 0.0,
            "angular_velocity_dps": 0.0,
            "left_slip_rate": 0.0,
            "right_slip_rate": 0.0,
            "active_surface": "normal",
            "collision_count": 0,
        }


def engine():
    device = FakeDeviceAdapter()
    world = FakeWorld()
    truth = FakeTruthObserver(device)
    value = PhysicalMazeEngine(
        device_adapter=device,
        world=world,
        truth_observer=truth,
        profile_repository=PhysicalProfileRepository(),
        profile_id="normal-v1",
        map_definition=default_map_definition(),
    )
    device.events.clear()
    return value, device, world, truth


def test_ready_declares_physical_devices_profile_and_map_capabilities():
    value, _device, _world, _truth = engine()

    ready = value.ready_message()

    assert ready["fw"] == "maze-webots-physical"
    assert ready["simulation_backend"] == "physical"
    assert ready["physical_profile_id"] == "normal-v1"
    assert len(ready["physical_profile_digest"]) == 64
    assert ready["map_digest"] == value.map_digest
    assert {
        "wheel_physics",
        "encoder",
        "tof_3way",
        "imu",
        "truth_evaluation_only",
    }.issubset(ready["features"])


def test_set_params_accepts_only_bounded_motion_and_estimation_values():
    value, _device, _world, _truth = engine()

    accepted = value.handle(
        {
            "type": "set_params",
            "seq": 1,
            "params": {
                "base_speed": 0.45,
                "pid_kp": 0.11,
                "heading_gain": 0.04,
            },
        },
        now_ms=0,
    )
    safety_rejected = value.handle(
        {
            "type": "set_params",
            "seq": 2,
            "params": {"danger_stop_mm": 1},
        },
        now_ms=0,
    )
    out_of_range = value.handle(
        {
            "type": "set_params",
            "seq": 3,
            "params": {"base_speed": 20},
        },
        now_ms=0,
    )

    assert accepted[0]["ok"] is True
    assert accepted[0]["param_version"] == 2
    assert value.params["base_speed"] == 0.45
    assert safety_rejected[0]["ok"] is False
    assert "not runtime-tunable" in safety_rejected[0]["message"]
    assert out_of_range[0]["ok"] is False


def test_profile_digest_is_verified_and_applied_only_on_reset_boundary():
    value, device, world, _truth = engine()
    low = PhysicalProfileRepository().get("low-v1")

    bad = value.handle(
        {
            "type": "load_profile",
            "seq": 1,
            "physical_profile_id": "low-v1",
            "digest": "0" * 64,
        },
        now_ms=0,
    )
    staged = value.handle(
        {
            "type": "load_profile",
            "seq": 2,
            "physical_profile_id": "low-v1",
            "digest": low.digest,
        },
        now_ms=0,
    )

    assert bad[0]["ok"] is False
    assert staged[0]["ok"] is True
    assert staged[0]["staged"] is True
    assert not any(event[0] == "profile" for event in world.events)

    reset = value.handle({"type": "reset", "seq": 3}, now_ms=8)

    assert reset[0]["ok"] is True
    assert value.profile.profile_id == "low-v1"
    assert ("profile", "low-v1") in world.events
    assert ("profile", "low-v1") in device.events
    assert ("reset",) in device.events
    assert world.events[-1] == ("refresh_devices",)


def test_load_map_keeps_version_and_digest_contract_until_atomic_reset():
    value, _device, world, _truth = engine()
    definition = validate_map_definition(
        {
            "rows": 2,
            "cols": 2,
            "cell_width_mm": 450,
            "cell_height_mm": 450,
            "wall_thickness_mm": 40,
            "wall_height_mm": 180,
            "start": {"x": 1, "y": 1, "heading": "W"},
            "goals": [{"x": 0, "y": 0}],
            "walls": [
                {"x1": 0, "y1": 0, "x2": 2, "y2": 0},
                {"x1": 2, "y1": 0, "x2": 2, "y2": 2},
                {"x1": 2, "y1": 2, "x2": 0, "y2": 2},
                {"x1": 0, "y1": 2, "x2": 0, "y2": 0},
            ],
            "source_image_digest": None,
        }
    )

    loaded = value.handle(
        {
            "type": "load_map",
            "seq": 10,
            "map_version_id": "map-2x2",
            "digest": definition.content_digest,
            "definition": definition.to_dict(),
        },
        now_ms=0,
    )

    assert loaded == [
        {
            "type": "ack",
            "seq": 10,
            "ok": True,
            "map_version_id": "map-2x2",
            "digest": definition.content_digest,
            "staged": True,
        }
    ]
    assert not any(event[0] == "map" for event in world.events)

    value.handle({"type": "reset", "seq": 11}, now_ms=8)

    assert value.map_version_id == "map-2x2"
    assert value.map_digest == definition.content_digest
    assert ("map", definition.content_digest) in world.events
    assert ("pose", (1, 1), "W") in world.events


def test_start_echoes_exact_map_and_profile_identity():
    value, _device, _world, _truth = engine()

    reply = value.handle(
        {
            "type": "start",
            "seq": 4,
            "map_version_id": value.map_version_id,
            "map_digest": value.map_digest,
            "physical_profile_id": value.profile.profile_id,
            "physical_profile_digest": value.profile.digest,
        },
        now_ms=0,
    )

    assert reply[0]["ok"] is True
    assert reply[0]["map_digest"] == value.map_digest
    assert reply[0]["physical_profile_digest"] == value.profile.digest


def test_start_rejects_a_loaded_map_with_unsafe_physical_passage():
    value, _device, _world, _truth = engine()
    definition = validate_map_definition(
        {
            "rows": 2,
            "cols": 2,
            "cell_width_mm": 300,
            "cell_height_mm": 300,
            "wall_thickness_mm": 20,
            "wall_height_mm": 180,
            "start": {"x": 0, "y": 1, "heading": "N"},
            "goals": [{"x": 1, "y": 0}],
            "walls": [
                {"x1": 0, "y1": 0, "x2": 2, "y2": 0},
                {"x1": 2, "y1": 0, "x2": 2, "y2": 2},
                {"x1": 2, "y1": 2, "x2": 0, "y2": 2},
                {"x1": 0, "y1": 2, "x2": 0, "y2": 0},
            ],
            "source_image_digest": None,
        }
    )
    loaded = value.handle(
        {
            "type": "load_map",
            "seq": 20,
            "map_version_id": "unsafe-physical-v1",
            "digest": definition.content_digest,
            "definition": definition.to_dict(),
        },
        now_ms=0,
    )
    value.handle({"type": "reset", "seq": 21}, now_ms=8)

    started = value.handle(
        {
            "type": "start",
            "seq": 22,
            "map_version_id": "unsafe-physical-v1",
            "map_digest": definition.content_digest,
            "physical_profile_id": value.profile.profile_id,
            "physical_profile_digest": value.profile.digest,
        },
        now_ms=16,
    )

    assert loaded[0]["ok"] is True
    assert started[0]["ok"] is False
    assert started[0]["code"] == "MAP_GEOMETRY_UNSAFE"
    assert started[0]["preflight"]["actual_passage_x_mm"] == 280.0
    assert started[0]["preflight"]["actual_passage_y_mm"] == 280.0


def test_action_acks_immediately_then_ticks_emit_telemetry_and_matching_done():
    value, device, _world, truth = engine()

    reply = value.handle(
        {
            "type": "action",
            "seq": 5,
            "action_id": "physical-1",
            "name": "move_cell",
            "target_ticks": 10,
            "speed": 0.3,
        },
        now_ms=0,
    )
    assert reply == [{"type": "ack", "seq": 5, "ok": True}]

    device.current_sample = sample(
        enc_left=10,
        enc_right=10,
        left_speed=0.0,
        right_speed=0.0,
    )
    messages = []
    for now_ms in (8, 16, 24, 32, 40, 48, 56):
        messages.extend(value.tick(now_ms=now_ms))

    done = [item for item in messages if item["type"] == "done"]
    telemetry = [
        item for item in messages if item["type"] == "telemetry"
    ]
    assert len(done) == 1
    assert done[0]["action_id"] == "physical-1"
    assert telemetry
    assert telemetry[-1]["simulation_backend"] == "physical"
    assert telemetry[-1]["sim_truth"]["x_mm"] == 100.0
    assert truth.calls > 0


def test_control_tick_is_eight_ms_while_telemetry_is_about_twenty_hz():
    value, _device, _world, _truth = engine()

    frames = []
    for now_ms in range(0, 161, 8):
        frames.extend(value.tick(now_ms=now_ms))
    telemetry_times = [
        frame["ts_ms"]
        for frame in frames
        if frame["type"] == "telemetry"
    ]

    assert telemetry_times == [0, 56, 112]
    assert value.control_tick_count == 21


def test_disconnect_heartbeat_timeout_and_close_stop_locally():
    value, device, _world, _truth = engine()
    value.handle(
        {
            "type": "action",
            "seq": 1,
            "action_id": "timeout-1",
            "name": "move_cell",
            "target_ticks": 500,
            "speed": 0.4,
        },
        now_ms=0,
    )

    timeout_frames = value.tick(now_ms=1300)

    assert any(
        frame.get("code") == "HEARTBEAT_TIMEOUT"
        for frame in timeout_frames
    )
    assert ("safe_stop",) in device.events

    device.events.clear()
    value.on_client_disconnected(now_ms=1400)
    value.close()

    assert device.events.count(("safe_stop",)) >= 2


def test_each_tick_orders_device_read_before_motor_write_before_truth_telemetry():
    value, device, _world, _truth = engine()

    value.tick(now_ms=0)

    names = [event[0] for event in device.events]
    assert names.index("sample") < names.index("command") < names.index("truth")
