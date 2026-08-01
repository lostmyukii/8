"""Physical Webots implementation of the simulated ESP32 protocol engine."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any, Mapping

from rdk_maze_tuner.core.maze_definition import MapDefinition
from rdk_maze_tuner.core.maze_validation import (
    MazeValidationError,
    validate_map_definition,
)
from simulation.webots.maze_car.map_loader import (
    CompiledMap,
    compile_map,
)
from simulation.webots.maze_car.physical_config import (
    PhysicalConfigError,
    PhysicalProfile,
    PhysicalProfileRepository,
)
from simulation.webots.maze_car.physical_preflight import (
    PhysicalGeometryPreflight,
)

from .action_controller import (
    ActionControlConfig,
    ActionControlOutput,
    ActionRejected,
    ActionRequest,
    PhysicalActionController,
)
from .physical_telemetry import PhysicalTelemetryProvider
from .physical_types import PhysicalDeviceError, PhysicalDeviceSample


_PARAM_LIMITS = {
    "base_speed": (0.05, 1.0),
    "turn_speed": (0.05, 1.0),
    "pid_kp": (0.0, 1.0),
    "pid_ki": (0.0, 1.0),
    "pid_kd": (0.0, 0.2),
    "heading_gain": (0.0, 1.0),
    "encoder_balance_gain": (0.0, 1.0),
    "position_tolerance_ticks": (1.0, 100.0),
    "angle_tolerance_deg": (0.5, 20.0),
    "settle_speed_rad_s": (0.05, 5.0),
    "slowdown_ticks": (10.0, 2000.0),
}
_INTEGER_PARAMS = frozenset(
    {"position_tolerance_ticks", "slowdown_ticks"}
)
_SAFETY_PARAMS = frozenset(
    {
        "danger_stop_mm",
        "action_timeout_ms",
        "heartbeat_timeout_ms",
        "stall_timeout_ms",
        "wheelspin_timeout_ms",
        "collision_accel_mps2",
    }
)


class PhysicalMazeEngine:
    """Own physical device sampling, action control, and protocol state."""

    def __init__(
        self,
        *,
        device_adapter: Any,
        world: Any,
        truth_observer: Any,
        profile_repository: PhysicalProfileRepository,
        profile_id: str,
        map_definition: MapDefinition,
        map_version_id: str = "builtin-open-5x5",
    ) -> None:
        self._device = device_adapter
        self._world = world
        self._truth_observer = truth_observer
        self._profiles = profile_repository
        self.profile = profile_repository.get(profile_id)
        self.map_definition = map_definition
        self.compiled_map = compile_map(map_definition)
        self.map_version_id = str(map_version_id)
        self.map_digest = map_definition.content_digest
        self.param_version = 1
        self.params: dict[str, Any] = {
            "base_speed": 0.1,
            "turn_speed": 0.5,
            "pid_kp": 0.03,
            "pid_ki": 0.005,
            "pid_kd": 0.0,
            "heading_gain": 0.03,
            "encoder_balance_gain": 0.025,
            "position_tolerance_ticks": 12,
            "angle_tolerance_deg": 2.0,
            "settle_speed_rad_s": 0.45,
            "slowdown_ticks": 240,
        }
        self._controller = self._new_controller()
        self._telemetry_provider = self._new_telemetry_provider()
        self._pending_profile: PhysicalProfile | None = None
        self._pending_map: tuple[str, MapDefinition, CompiledMap] | None = None
        self._latest_sample = self._device.sample(timestamp_ms=0)
        self._latest_sample.require_finite()
        self._latest_output = self._controller.tick(
            sample=self._latest_sample,
            now_ms=0,
        )
        self._last_now_ms = 0
        self._last_telemetry_ms = -50
        self._collision_count = 0
        self.control_tick_count = 0
        self._closed = False

    def ready_message(self) -> dict[str, Any]:
        return {
            "type": "ready",
            "fw": "maze-webots-physical",
            "version": "0.2.0",
            "simulated": True,
            "simulation_backend": "physical",
            "imu_available": True,
            "features": [
                "wheel_physics",
                "encoder",
                "tof_3way",
                "imu",
                "json_serial",
                "truth_evaluation_only",
                "physical_profiles",
            ],
            "physical_profile_id": self.profile.profile_id,
            "physical_profile_digest": self.profile.digest,
            "map_version_id": self.map_version_id,
            "map_digest": self.map_digest,
        }

    def telemetry_message(self) -> dict[str, Any]:
        return self._build_telemetry(
            self._latest_sample,
            self._latest_output,
        )

    def on_client_connected(self, *, now_ms: int) -> None:
        self._controller.heartbeat(now_ms=now_ms)

    def on_client_disconnected(self, *, now_ms: int) -> None:
        self._device.safe_stop()
        self._controller.reset()
        self._last_now_ms = int(now_ms)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._device.safe_stop()
        self._controller.reset()

    def handle(
        self,
        message: Mapping[str, Any],
        *,
        now_ms: int,
    ) -> list[dict[str, Any]]:
        self._last_now_ms = int(now_ms)
        message_type = str(message.get("type") or "")
        seq = int(message.get("seq") or 0)
        if message_type == "heartbeat":
            self._controller.heartbeat(now_ms=now_ms)
            return [self._ack(seq)]
        if message_type == "set_params":
            return [self._set_params(message, seq=seq)]
        if message_type == "load_profile":
            return [self._stage_profile(message, seq=seq)]
        if message_type == "load_map":
            return [self._stage_map(message, seq=seq)]
        if message_type == "reset":
            return self._reset(message, seq=seq, now_ms=now_ms)
        if message_type == "start":
            return [self._start(message, seq=seq)]
        if message_type == "action":
            return [self._start_action(message, seq=seq, now_ms=now_ms)]
        if message_type == "pause":
            self._controller.pause()
            return [self._ack(seq)]
        if message_type == "stop":
            self._controller.stop()
            return [self._ack(seq)]
        if message_type == "estop":
            output = self._controller.estop(now_ms=now_ms)
            self._latest_output = output
            self._device.safe_stop()
            return [
                self._ack(seq),
                *([output.event] if output.event else []),
                self._build_telemetry(self._latest_sample, output),
            ]
        if message_type == "clear_estop":
            cleared = self._controller.clear_estop()
            return [
                self._ack(
                    seq,
                    ok=cleared,
                    message=None if cleared else "estop is not active",
                )
            ]
        return [
            self._ack(
                seq,
                ok=False,
                message=(
                    f"unsupported message type: "
                    f"{message_type or 'missing'}"
                ),
            )
        ]

    def tick(self, *, now_ms: int) -> list[dict[str, Any]]:
        self._last_now_ms = int(now_ms)
        self.control_tick_count += 1
        messages: list[dict[str, Any]] = []
        try:
            sample = self._device.sample(timestamp_ms=now_ms)
            sample.require_finite()
            output = self._controller.tick(sample=sample, now_ms=now_ms)
            self._device.command_wheels(
                left_velocity_rad_s=output.motor_velocity_left_rad_s,
                right_velocity_rad_s=output.motor_velocity_right_rad_s,
                torque_nm=output.motor_available_torque_nm,
            )
        except PhysicalDeviceError as exc:
            self._device.safe_stop()
            self._controller.reset()
            return [
                {
                    "type": "error",
                    "action_id": None,
                    "code": exc.code,
                    "message": str(exc),
                    "simulated": True,
                }
            ]
        self._latest_sample = sample
        self._latest_output = output
        if output.event is not None:
            event = dict(output.event)
            event["simulated"] = True
            if event.get("code") == "COLLISION_SUSPECTED":
                self._collision_count += 1
            messages.append(event)
            if event["type"] == "error":
                self._device.safe_stop()
        if now_ms - self._last_telemetry_ms >= 50:
            self._last_telemetry_ms = int(now_ms)
            messages.append(self._build_telemetry(sample, output))
        return messages

    def _set_params(
        self,
        message: Mapping[str, Any],
        *,
        seq: int,
    ) -> dict[str, Any]:
        if self._controller.active:
            return self._ack(
                seq,
                ok=False,
                message="cannot set params while an action is active",
            )
        values = message.get("params")
        if not isinstance(values, Mapping):
            return self._ack(
                seq,
                ok=False,
                message="params must be an object",
            )
        staged = dict(self.params)
        for raw_name, raw_value in values.items():
            name = str(raw_name)
            if name in _SAFETY_PARAMS or name not in _PARAM_LIMITS:
                return self._ack(
                    seq,
                    ok=False,
                    message=f"{name} is not runtime-tunable",
                )
            if isinstance(raw_value, bool):
                return self._ack(
                    seq,
                    ok=False,
                    message=f"{name} must be numeric",
                )
            try:
                number = float(raw_value)
            except (TypeError, ValueError):
                return self._ack(
                    seq,
                    ok=False,
                    message=f"{name} must be numeric",
                )
            low, high = _PARAM_LIMITS[name]
            if not math.isfinite(number) or not low <= number <= high:
                return self._ack(
                    seq,
                    ok=False,
                    message=f"{name} must be between {low} and {high}",
                )
            staged[name] = (
                int(round(number)) if name in _INTEGER_PARAMS else number
            )
        self.params = staged
        self.param_version += 1
        self._controller = self._new_controller()
        return self._ack(
            seq,
            param_version=self.param_version,
        )

    def _stage_profile(
        self,
        message: Mapping[str, Any],
        *,
        seq: int,
    ) -> dict[str, Any]:
        if self._controller.active:
            return self._ack(
                seq,
                ok=False,
                message="cannot load profile while an action is active",
            )
        profile_id = str(message.get("physical_profile_id") or "")
        digest = str(message.get("digest") or "")
        try:
            profile = self._profiles.get(
                profile_id,
                expected_digest=digest,
            )
        except PhysicalConfigError as exc:
            return self._ack(seq, ok=False, message=str(exc))
        self._pending_profile = profile
        return self._ack(
            seq,
            physical_profile_id=profile.profile_id,
            digest=profile.digest,
            staged=True,
        )

    def _stage_map(
        self,
        message: Mapping[str, Any],
        *,
        seq: int,
    ) -> dict[str, Any]:
        if self._controller.active:
            return self._ack(
                seq,
                ok=False,
                message="cannot load map while an action is active",
            )
        version_id = str(message.get("map_version_id") or "")
        digest = str(message.get("digest") or "")
        if not version_id:
            return self._ack(
                seq,
                ok=False,
                message="map_version_id is required",
            )
        try:
            definition = validate_map_definition(message.get("definition"))
        except (MazeValidationError, TypeError) as exc:
            return self._ack(seq, ok=False, message=str(exc))
        if definition.content_digest != digest:
            return self._ack(
                seq,
                ok=False,
                message="map digest does not match definition",
            )
        compiled = compile_map(definition)
        self._pending_map = (version_id, definition, compiled)
        return self._ack(
            seq,
            map_version_id=version_id,
            digest=digest,
            staged=True,
        )

    def _reset(
        self,
        message: Mapping[str, Any],
        *,
        seq: int,
        now_ms: int,
    ) -> list[dict[str, Any]]:
        self._device.safe_stop()
        self._controller.reset()
        if self._pending_profile is not None:
            self.profile = self._pending_profile
            self._pending_profile = None
        if self._pending_map is not None:
            (
                self.map_version_id,
                self.map_definition,
                self.compiled_map,
            ) = self._pending_map
            self.map_digest = self.map_definition.content_digest
            self._pending_map = None
        self._world.apply_profile(self.profile)
        self.compiled_map = self._world.load_map(self.map_definition)
        self._world.reset_pose(self.compiled_map)
        # Profile edits and Node.loadState() can regenerate nested devices.
        # Rebind after the world reset, then advance enough simulation steps
        # for newly enabled sensors to publish a fresh first sample.
        self._device.apply_profile(self.profile)
        self._world.refresh_device_samples()
        self._controller = self._new_controller()
        self._telemetry_provider = self._new_telemetry_provider()
        self._collision_count = 0
        self._latest_sample = self._device.sample(timestamp_ms=now_ms)
        self._latest_output = self._controller.tick(
            sample=self._latest_sample,
            now_ms=now_ms,
        )
        self._last_telemetry_ms = int(now_ms)
        return [
            self._identity_ack(seq),
            self._build_telemetry(
                self._latest_sample,
                self._latest_output,
            ),
        ]

    def _start(
        self,
        message: Mapping[str, Any],
        *,
        seq: int,
    ) -> dict[str, Any]:
        mismatch = self._identity_mismatch(message)
        if mismatch is not None:
            return self._ack(seq, ok=False, message=mismatch)
        if self._controller.active or self._controller.state != "IDLE":
            return self._ack(
                seq,
                ok=False,
                message="physical controller is not idle",
            )
        geometry = self.profile.geometry
        preflight = PhysicalGeometryPreflight(
            chassis_length_mm=geometry.chassis_length_m * 1_000.0,
            chassis_width_mm=geometry.chassis_width_m * 1_000.0,
        ).check(
            self.map_definition,
            map_version_id=self.map_version_id,
        )
        if not preflight.ok:
            return self._ack(
                seq,
                ok=False,
                code=preflight.code,
                message="physical map passage is unsafe",
                preflight=preflight.to_dict(),
            )
        return self._identity_ack(seq)

    def _start_action(
        self,
        message: Mapping[str, Any],
        *,
        seq: int,
        now_ms: int,
    ) -> dict[str, Any]:
        name = str(message.get("name") or "")
        speed = message.get("speed")
        if speed is None:
            speed = (
                self.params["base_speed"]
                if name == "move_cell"
                else self.params["turn_speed"]
            )
        try:
            request = ActionRequest(
                action_id=str(message.get("action_id") or ""),
                name=name,
                target_ticks=int(message.get("target_ticks") or 0),
                speed=float(speed),
            )
            self._controller.start(
                request,
                sample=self._latest_sample,
                now_ms=now_ms,
            )
        except (ActionRejected, TypeError, ValueError) as exc:
            return self._ack(seq, ok=False, message=str(exc))
        return self._ack(seq)

    def _new_controller(self) -> PhysicalActionController:
        config = replace(
            ActionControlConfig(),
            pid_kp=float(self.params["pid_kp"]),
            pid_ki=float(self.params["pid_ki"]),
            pid_kd=float(self.params["pid_kd"]),
            heading_gain=float(self.params["heading_gain"]),
            encoder_balance_gain=float(
                self.params["encoder_balance_gain"]
            ),
            position_tolerance_ticks=int(
                self.params["position_tolerance_ticks"]
            ),
            angle_tolerance_deg=float(
                self.params["angle_tolerance_deg"]
            ),
            settle_speed_rad_s=float(
                self.params["settle_speed_rad_s"]
            ),
            slowdown_ticks=int(self.params["slowdown_ticks"]),
        )
        return PhysicalActionController(
            profile=self.profile,
            config=config,
        )

    def _new_telemetry_provider(self) -> PhysicalTelemetryProvider:
        return PhysicalTelemetryProvider(
            profile_id=self.profile.profile_id,
            profile_digest=self.profile.digest,
        )

    def _build_telemetry(
        self,
        sample: PhysicalDeviceSample,
        output: ActionControlOutput,
    ) -> dict[str, Any]:
        telemetry = self._telemetry_provider.build(sample)
        telemetry.update(
            {
                "state": output.state,
                "pwm_left": output.pwm_left,
                "pwm_right": output.pwm_right,
                "motor_available_torque_nm": (
                    output.motor_available_torque_nm
                ),
                "motor_torque_left_nm": output.motor_torque_left_nm,
                "motor_torque_right_nm": output.motor_torque_right_nm,
                "param_version": self.param_version,
                "map_version_id": self.map_version_id,
                "map_digest": self.map_digest,
                **output.telemetry,
            }
        )
        telemetry["sim_truth"] = self._truth_observer.observe(
            wheel_linear_left_mps=(
                sample.wheel_speed_left_rad_s
                * self.profile.geometry.wheel_radius_m
            ),
            wheel_linear_right_mps=(
                sample.wheel_speed_right_rad_s
                * self.profile.geometry.wheel_radius_m
            ),
            axle_track_m=self.profile.geometry.axle_track_m,
            active_surface=sample.friction_profile,
            collision_count=self._collision_count,
        )
        return telemetry

    def _identity_mismatch(
        self,
        message: Mapping[str, Any],
    ) -> str | None:
        checks = (
            (
                "map_version_id",
                self.map_version_id,
                "requested map version is not loaded",
            ),
            (
                "map_digest",
                self.map_digest,
                "requested map digest is not loaded",
            ),
            (
                "physical_profile_id",
                self.profile.profile_id,
                "requested physical profile is not loaded",
            ),
            (
                "physical_profile_digest",
                self.profile.digest,
                "requested physical profile digest is not loaded",
            ),
        )
        for name, actual, error in checks:
            requested = str(message.get(name) or "")
            if requested and requested != actual:
                return error
        return None

    def _identity_ack(self, seq: int) -> dict[str, Any]:
        return self._ack(
            seq,
            map_version_id=self.map_version_id,
            map_digest=self.map_digest,
            physical_profile_id=self.profile.profile_id,
            physical_profile_digest=self.profile.digest,
        )

    @staticmethod
    def _ack(
        seq: int,
        *,
        ok: bool = True,
        message: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "type": "ack",
            "seq": int(seq),
            "ok": bool(ok),
        }
        if message:
            result["message"] = message
        result.update(fields)
        return result
