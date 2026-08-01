"""Dashboard state container shared by HTTP and WebSocket handlers."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Deque, Mapping, Optional

from rdk_maze_tuner.core.maze_map import Direction, MazeMap, PlannedAction
from rdk_maze_tuner.core.param_manager import ParamManager, ParamValidationError
from rdk_maze_tuner.core.device_session import DeviceSession
from rdk_maze_tuner.core.pose_fusion import PoseFusion
from rdk_maze_tuner.core.pose_types import (
    PoseFusionConfig,
    PoseObservation,
    TruthPose,
    WallConstraint,
    evaluate_pose,
)
from rdk_maze_tuner.core.protocol import (
    ProtocolError,
    extract_fusion_telemetry,
    extract_simulation_truth,
)
from rdk_maze_tuner.core.serial_client import SerialClientError
from rdk_maze_tuner.core.slip_estimator import (
    SlipEstimator,
    SlipEstimatorConfig,
)


DEFAULT_TELEMETRY = {
    "state": "OFFLINE",
    "front_mm": None,
    "left_mm": None,
    "right_mm": None,
    "enc_left": 0,
    "enc_right": 0,
    "pwm_left": 0,
    "pwm_right": 0,
    "param_version": 1,
    "imu_available": False,
    "pose_confidence": 0.0,
    "pose_quality_flags": ["uninitialized"],
    "slip_quality": "insufficient",
}

SIMULATION_ONLY_TELEMETRY_FIELDS = (
    "sim_truth",
    "sim_cell",
    "sim_heading",
    "simulated",
    "fusion_front_mm",
    "fusion_left_mm",
    "fusion_right_mm",
    "truth_error_cm",
    "truth_yaw_error_deg",
    "truth_evaluation_only",
    "truth_quality",
)


class DashboardState:
    def __init__(
        self,
        *,
        params: ParamManager,
        maze: Optional[MazeMap] = None,
        client: Optional[DeviceSession] = None,
        max_logs: int = 200,
        clock_ms: Optional[Callable[[], int]] = None,
    ) -> None:
        self.params = params
        self.maze = maze or MazeMap(wall_threshold_mm=int(params.get("tof.wall_threshold_mm")))
        self.client = client
        self.auto_tune_enabled = True
        self.current_action: Optional[dict[str, Any]] = None
        self.last_ack: Optional[dict[str, Any]] = None
        self.last_param_event: Optional[dict[str, Any]] = None
        self.telemetry: dict[str, Any] = dict(DEFAULT_TELEMETRY)
        self.logs: Deque[dict[str, Any]] = deque(maxlen=max_logs)
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._lock = RLock()
        self._manual_action_index = 0
        self._task_orchestrator: Any = None
        self._front_wall_reference: tuple[str, float] | None = None
        self._reset_pose_models_locked()

    def attach_task_orchestrator(self, orchestrator: Any) -> None:
        with self._lock:
            self._task_orchestrator = orchestrator

    def set_maze(self, maze: MazeMap) -> None:
        with self._lock:
            self.maze = maze
            self._reset_pose_models_locked()

    @property
    def connected(self) -> bool:
        if self.client is None:
            return False
        return bool(getattr(self.client, "connected", True))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            tasks = (
                []
                if self._task_orchestrator is None
                else self._task_orchestrator.list_tasks()
            )
            return {
                "connected": self.connected,
                "telemetry": _json_ready(self.telemetry),
                "current_action": _json_ready(self.current_action),
                "last_ack": _json_ready(self.last_ack),
                "last_param_event": _json_ready(self.last_param_event),
                "params": self.params.snapshot(),
                "maze": self.maze.to_dict(),
                "pose": self.pose_estimate.to_dict(),
                "slip": self.slip_estimate.to_dict(),
                "auto_tune_enabled": self.auto_tune_enabled,
                "logs": list(self.logs),
                "tasks": tasks,
            }

    def update_telemetry(self, telemetry: Mapping[str, Any]) -> None:
        with self._lock:
            self._update_fused_telemetry_locked(telemetry)
            self.record("telemetry", self.telemetry)

    def handle_device_message(
        self,
        message: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            self._handle_serial_message_locked(message)
            return dict(message)

    def handle_device_disconnect(self, message: str) -> dict[str, Any]:
        event = {
            "type": "error",
            "code": "DEVICE_DISCONNECTED",
            "message": message,
        }
        with self._lock:
            self.telemetry["state"] = "OFFLINE"
            self.current_action = event
            self.record("error", event)
        return event

    def send_heartbeat(self) -> dict[str, Any]:
        client = self.client
        if client is None:
            return {"ok": False, "sent_to_esp32": False, "ack": None}
        ack = client.send_heartbeat(ts_ms=self._clock_ms())
        with self._lock:
            self.last_ack = dict(ack)
            row = self.record("heartbeat", {"sent_to_esp32": True, "ack": ack})
            return {"ok": True, "sent_to_esp32": True, "ack": _json_ready(ack), "log": row}

    def apply_param_updates(self, updates: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(updates, Mapping) or not updates:
            raise ParamValidationError("updates must be a non-empty object")

        with self._lock:
            event = self.params.apply_updates(dict(updates), source="dashboard")
            params_for_device = self.params.esp32_params()
            client = self.client

        ack = None
        sent = False
        if client is not None:
            ack = client.send_params(params_for_device)
            sent = True

        with self._lock:
            if ack is not None:
                self.last_ack = dict(ack)
                self.record("ack", ack)
            self.last_param_event = event
            self.record("param_change", event)
            return {
                "ok": True,
                "sent_to_esp32": sent,
                "ack": _json_ready(ack),
                "param_event": event,
                "params": self.params.snapshot(),
            }

    def set_auto_tune(self, enabled: bool) -> dict[str, Any]:
        with self._lock:
            self.auto_tune_enabled = bool(enabled)
            event = {"name": "auto_tune", "enabled": self.auto_tune_enabled}
            self.record("command", event)
            return {"ok": True, "auto_tune_enabled": self.auto_tune_enabled}

    def estop(self, *, reason: str = "dashboard") -> dict[str, Any]:
        return self._send_control_command("estop", reason=reason)

    def stop(self, *, reason: str = "dashboard") -> dict[str, Any]:
        return self._send_control_command("stop", reason=reason)

    def manual_action(self, *, name: str) -> dict[str, Any]:
        with self._lock:
            action_id = self._next_manual_action_id()
            speed, target_ticks = self._motion_params_for(name)
            command = {
                "type": "action",
                "action_id": action_id,
                "name": name,
                "reason": "dashboard",
                "speed": speed,
                "target_ticks": target_ticks,
            }
            self.current_action = command
            self.record("planned_action", command)
            client = self.client
        if client is None:
            return {
                "ok": False,
                "command": name,
                "action_id": action_id,
                "sent_to_esp32": False,
                "ack": None,
                "result": None,
            }

        try:
            ack, result = client.execute_action_with_ack(
                action_id=action_id,
                name=name,
                speed=speed,
                target_ticks=target_ticks,
            )
        except SerialClientError as exc:
            with self._lock:
                result = {
                    "type": "error",
                    "action_id": action_id,
                    "name": name,
                    "code": "SERIAL_CLIENT_ERROR",
                    "message": str(exc),
                }
                self.current_action = result
                self.record("error", result)
                return {
                    "ok": False,
                    "command": name,
                    "action_id": action_id,
                    "sent_to_esp32": True,
                    "ack": None,
                    "result": result,
                }

        with self._lock:
            if ack is not None:
                self.last_ack = dict(ack)
                self.record("ack", ack)

            self.current_action = dict(result)
            if result.get("type") == "done" and result.get("success") is not False:
                self.maze.apply_completed_action(PlannedAction(name=name))
                self.record("done", result)
                return {
                    "ok": True,
                    "command": name,
                    "action_id": action_id,
                    "sent_to_esp32": True,
                    "ack": _json_ready(ack),
                    "result": _json_ready(result),
                }

            self.record("error", result)
            return {
                "ok": False,
                "command": name,
                "action_id": action_id,
                "sent_to_esp32": True,
                "ack": _json_ready(ack),
                "result": _json_ready(result),
            }

    def _send_control_command(self, name: str, *, reason: str) -> dict[str, Any]:
        client = self.client
        ack = None
        sent = False
        if client is not None:
            if name == "estop":
                ack = client.estop(reason=reason)
            elif name == "stop":
                ack = client.stop()
            else:
                raise SerialClientError(f"unsupported control command: {name}")
            sent = True

        with self._lock:
            if ack is not None:
                self.last_ack = dict(ack)
                self.record("ack", ack)
            command = {
                "name": name,
                "reason": reason,
                "sent_to_esp32": sent,
                "ack": _json_ready(ack),
            }
            self.current_action = command
            self.record("command", command)
            return {
                "ok": True,
                "command": name,
                "sent_to_esp32": sent,
                "ack": _json_ready(ack),
            }

    def record(self, event_type: str, payload: Any) -> dict[str, Any]:
        row = {
            "ts_ms": self._clock_ms(),
            "type": event_type,
            "payload": _json_ready(payload),
        }
        self.logs.append(row)
        return row

    def _handle_serial_message_locked(self, message: Mapping[str, Any]) -> None:
        message_type = str(message.get("type") or "serial")
        if message_type == "telemetry":
            self._update_fused_telemetry_locked(message)
            self.record("telemetry", self.telemetry)
            return
        if message_type == "ack":
            self.last_ack = dict(message)
            self.record("ack", message)
            return
        if message_type in {"done", "error"}:
            self.current_action = dict(message)
            self.record(message_type, message)
            return
        if message_type == "ready":
            self.record("ready", message)
            return
        self.record("serial", message)

    def _next_manual_action_id(self) -> str:
        self._manual_action_index += 1
        return f"dashboard-{self._manual_action_index:04d}"

    def _reset_pose_models_locked(self) -> None:
        cell_size_mm = float(
            self.params.get("robot.cell_size_cm")
        ) * 10.0
        cell_width_mm = float(
            self.maze.cell_width_mm or cell_size_mm
        )
        cell_height_mm = float(
            self.maze.cell_height_mm or cell_size_mm
        )
        config = PoseFusionConfig(
            cell_width_mm=cell_width_mm,
            cell_height_mm=cell_height_mm,
            cell_ticks=float(self.params.get("motion.cell_ticks")),
            turn_90_ticks=float(
                self.params.get("motion.turn_90_ticks")
            ),
            wheel_base_mm=float(
                self.params.get("robot.wheel_base_cm")
            )
            * 10.0,
            y_axis_down=bool(
                getattr(self.maze, "_screen_coordinates", False)
            ),
        )
        self.pose_fusion = PoseFusion(
            config=config,
            initial_cell=self.maze.position,
            initial_heading=self.maze.heading.value,
        )
        self.slip_estimator = SlipEstimator(
            SlipEstimatorConfig(
                mm_per_tick=config.mm_per_tick,
                wheel_base_mm=config.wheel_base_mm,
            )
        )
        self.pose_estimate = self.pose_fusion.estimate()
        self.slip_estimate = self.slip_estimator.estimate()
        self._front_wall_reference = None

    def _update_fused_telemetry_locked(
        self,
        message: Mapping[str, Any],
    ) -> None:
        incoming = dict(message)
        merged = dict(self.telemetry)
        merged.update(incoming)
        if incoming.get("simulated") is not True:
            for field in SIMULATION_ONLY_TELEMETRY_FIELDS:
                if field not in incoming:
                    merged.pop(field, None)
        evidence = extract_fusion_telemetry(merged)
        if "ts_ms" not in evidence and "uptime_ms" not in evidence:
            evidence["ts_ms"] = self._clock_ms()

        maze_heading = self.maze.heading.value
        if (
            self.pose_estimate.grid_cell != self.maze.position
            or self.pose_estimate.heading != maze_heading
        ):
            self.pose_estimate = self.pose_fusion.anchor_grid(
                self.maze.position,
                maze_heading,
            )

        constraints, front_reference = (
            self._wall_constraints_locked(evidence)
        )
        observation = PoseObservation.from_mapping(evidence)
        self.pose_estimate = self.pose_fusion.update(
            observation,
            wall_constraints=constraints,
        )
        external_distance = self._external_wall_motion(
            front_reference
        )
        self.slip_estimate = self.slip_estimator.update(
            timestamp_ms=observation.timestamp_ms,
            enc_left=observation.enc_left,
            enc_right=observation.enc_right,
            imu_available=observation.imu_available,
            imu_yaw_deg=observation.imu_yaw_deg,
            external_distance_mm=external_distance,
        )

        pose = self.pose_estimate
        slip = self.slip_estimate
        merged.update(
            {
                "x_mm": pose.x_mm,
                "y_mm": pose.y_mm,
                "x_cm": round(pose.x_mm / 10.0, 3),
                "y_cm": round(pose.y_mm / 10.0, 3),
                "yaw_deg": pose.yaw_deg,
                "speed_mm_s": pose.speed_mm_s,
                "speed_cm_s": round(pose.speed_mm_s / 10.0, 3),
                "angular_velocity_dps": pose.angular_velocity_dps,
                "pose_confidence": pose.confidence,
                "pose_covariance": list(pose.covariance),
                "pose_correction_reason": pose.correction_source,
                "pose_quality_flags": list(pose.quality_flags),
                "continuous_heading_valid": (
                    pose.continuous_heading_valid
                ),
                "wall_residual_mm": pose.wall_residual_mm,
                "slip_left": slip.left_slip_rate,
                "slip_right": slip.right_slip_rate,
                "slip_rate": slip.overall_slip_rate,
                "equivalent_friction": slip.equivalent_friction,
                "friction_profile": slip.friction_profile,
                "slip_quality": slip.quality,
                "slip_confidence": slip.confidence,
                "slip_quality_flags": list(slip.quality_flags),
                "friction_is_estimate": True,
            }
        )
        try:
            truth_payload = extract_simulation_truth(merged)
            if truth_payload is not None:
                evaluation = evaluate_pose(
                    pose,
                    TruthPose.from_mapping(truth_payload),
                )
                merged["truth_error_cm"] = round(
                    evaluation.position_error_mm / 10.0,
                    3,
                )
                merged["truth_yaw_error_deg"] = (
                    evaluation.yaw_error_deg
                )
                merged["truth_evaluation_only"] = True
        except (ProtocolError, ValueError, KeyError):
            merged["truth_quality"] = "invalid"
        self.telemetry = merged

    def _wall_constraints_locked(
        self,
        telemetry: Mapping[str, Any],
    ) -> tuple[
        tuple[WallConstraint, ...],
        tuple[str, float] | None,
    ]:
        if not getattr(self.maze, "_screen_coordinates", False):
            return (), None
        width = float(
            self.maze.cell_width_mm
            or self.pose_fusion.config.cell_width_mm
        )
        height = float(
            self.maze.cell_height_mm
            or self.pose_fusion.config.cell_height_mm
        )
        x, y = self.maze.position
        constraints = []
        front_reference = None
        for local, fusion_field, raw_field in (
            ("front", "fusion_front_mm", "front_mm"),
            ("left", "fusion_left_mm", "left_mm"),
            ("right", "fusion_right_mm", "right_mm"),
        ):
            value = telemetry.get(fusion_field)
            if value is None:
                value = telemetry.get(raw_field)
            try:
                distance = float(value)
            except (TypeError, ValueError):
                continue
            if not 0.0 <= distance <= 5000.0:
                continue
            direction = self.maze.local_to_global(local)
            coordinate = self._nearest_wall_coordinate_locked(
                direction,
                start=(x, y),
                width_mm=width,
                height_mm=height,
            )
            if coordinate is None:
                continue
            constraint = WallConstraint(
                direction=direction.value,
                wall_coordinate_mm=coordinate,
                distance_mm=distance,
                variance_mm2=625.0,
            )
            constraints.append(constraint)
            if local == "front":
                front_reference = (
                    f"{direction.value}:{coordinate}",
                    distance,
                )
        return tuple(constraints), front_reference

    def _nearest_wall_coordinate_locked(
        self,
        direction: Direction,
        *,
        start: tuple[int, int],
        width_mm: float,
        height_mm: float,
    ) -> float | None:
        rows = self.maze.rows
        cols = self.maze.cols
        if rows is None or cols is None:
            return None
        cell = start
        for _ in range(rows * cols + 1):
            x, y = cell
            if not 0 <= x < cols or not 0 <= y < rows:
                return None
            wall = self.maze.cell(cell).walls[direction.value]
            if wall is True:
                return {
                    "N": y * height_mm,
                    "S": (y + 1) * height_mm,
                    "W": x * width_mm,
                    "E": (x + 1) * width_mm,
                }[direction.value]
            if wall is not False:
                return None
            cell = self.maze.neighbor(cell, direction)
        return None

    def _external_wall_motion(
        self,
        current: tuple[str, float] | None,
    ) -> float | None:
        previous = self._front_wall_reference
        self._front_wall_reference = current
        if current is None or previous is None:
            return None
        if current[0] != previous[0]:
            return None
        return previous[1] - current[1]

    def _motion_params_for(self, action_name: str) -> tuple[float, int]:
        if action_name == "move_cell":
            return float(self.params.get("motor.base_speed")), int(self.params.get("motion.cell_ticks"))
        if action_name == "turn_back":
            return float(self.params.get("motor.turn_speed")), int(self.params.get("motion.turn_180_ticks"))
        if action_name in {"turn_left", "turn_right"}:
            return float(self.params.get("motor.turn_speed")), int(self.params.get("motion.turn_90_ticks"))
        raise SerialClientError(f"unsupported manual action: {action_name}")


def _json_ready(value: Any) -> Any:
    if is_dataclass(value):
        return _json_ready(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, deque)):
        return [_json_ready(item) for item in value]
    return value
