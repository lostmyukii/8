"""Dashboard state container shared by HTTP and WebSocket handlers."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Deque, Mapping, Optional

from rdk_maze_tuner.core.maze_map import MazeMap, PlannedAction
from rdk_maze_tuner.core.param_manager import ParamManager, ParamValidationError
from rdk_maze_tuner.core.device_session import DeviceSession
from rdk_maze_tuner.core.serial_client import SerialClientError


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
}


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

    @property
    def connected(self) -> bool:
        if self.client is None:
            return False
        return bool(getattr(self.client, "connected", True))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "connected": self.connected,
                "telemetry": _json_ready(self.telemetry),
                "current_action": _json_ready(self.current_action),
                "last_ack": _json_ready(self.last_ack),
                "last_param_event": _json_ready(self.last_param_event),
                "params": self.params.snapshot(),
                "maze": self.maze.to_dict(),
                "auto_tune_enabled": self.auto_tune_enabled,
                "logs": list(self.logs),
            }

    def update_telemetry(self, telemetry: Mapping[str, Any]) -> None:
        with self._lock:
            self.telemetry.update(dict(telemetry))
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
            self.telemetry.update(dict(message))
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
