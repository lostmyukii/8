"""Deterministic action-level model of the ESP32 maze-car contract."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping


HEADINGS = ("N", "E", "S", "W")
DELTAS = {
    "N": (0, -1),
    "E": (1, 0),
    "S": (0, 1),
    "W": (-1, 0),
}
HEADING_ANGLES = {
    "N": 0.0,
    "E": -math.pi / 2,
    "S": math.pi,
    "W": math.pi / 2,
}

Cell = tuple[int, int]
Edge = frozenset[Cell]


def _edge(a: Cell, b: Cell) -> Edge:
    return frozenset((a, b))


INTERNAL_WALLS = {
    _edge((0, 1), (1, 1)),
    _edge((0, 3), (1, 3)),
    _edge((1, 2), (1, 3)),
    _edge((2, 1), (3, 1)),
    _edge((2, 2), (2, 3)),
    _edge((3, 0), (3, 1)),
    _edge((3, 3), (4, 3)),
    _edge((4, 1), (4, 2)),
}


@dataclass
class PendingAction:
    action_id: str
    name: str
    target_ticks: int
    start_ms: int
    duration_ms: int
    start_cell: Cell
    target_cell: Cell
    start_heading: int
    target_heading: int
    angle_delta: float


class MazeSimEngine:
    """Implements the newline-JSON behavior expected from the ESP32."""

    width = 5
    height = 5
    cell_size_m = 0.45

    def __init__(self) -> None:
        self.params: dict[str, Any] = {}
        self.param_version = 1
        self._reset_state()

    def _reset_state(self) -> None:
        self.cell: Cell = (0, 4)
        self.heading_index = 0
        self.state = "IDLE"
        self.estopped = False
        self.enc_left = 0
        self.enc_right = 0
        self.pending: PendingAction | None = None
        self.last_now_ms = 0
        self.last_telemetry_ms = -100

    @property
    def heading(self) -> str:
        return HEADINGS[self.heading_index]

    def ready_message(self) -> dict[str, Any]:
        return {
            "type": "ready",
            "fw": "maze-webots-sim",
            "version": "0.1.0",
            "simulated": True,
        }

    def handle(self, message: Mapping[str, Any], *, now_ms: int) -> list[dict[str, Any]]:
        self.last_now_ms = now_ms
        message_type = str(message.get("type") or "")
        seq = int(message.get("seq") or 0)

        if message_type == "heartbeat":
            return [self._ack(seq)]
        if message_type == "set_params":
            params = message.get("params")
            if not isinstance(params, Mapping):
                return [self._ack(seq, ok=False, message="params must be an object")]
            self.params.update(dict(params))
            self.param_version += 1
            return [self._ack(seq)]
        if message_type == "reset":
            self._reset_state()
            return [self._ack(seq), self.telemetry_message()]
        if message_type == "start":
            if self.estopped:
                return [
                    self._ack(
                        seq,
                        ok=False,
                        message="simulation is in ESTOP",
                    )
                ]
            self.state = "IDLE"
            return [self._ack(seq), self.telemetry_message()]
        if message_type == "pause":
            cancelled = self._cancel_pending(
                "PAUSED",
                "pause command",
            )
            self.state = "PAUSED"
            return [self._ack(seq), *cancelled, self.telemetry_message()]
        if message_type == "stop":
            cancelled = self._cancel_pending("STOPPED", "stop command")
            self.state = "IDLE"
            return [self._ack(seq), *cancelled, self.telemetry_message()]
        if message_type == "estop":
            cancelled = self._cancel_pending("ESTOP", str(message.get("reason") or "estop"))
            self.estopped = True
            self.state = "ESTOP"
            return [self._ack(seq), *cancelled, self.telemetry_message()]
        if message_type == "clear_estop":
            self.estopped = False
            self.state = "IDLE"
            return [self._ack(seq), self.telemetry_message()]
        if message_type == "action":
            return self._start_action(message, seq=seq, now_ms=now_ms)
        return [self._ack(seq, ok=False, message=f"unsupported message type: {message_type or 'missing'}")]

    def tick(self, *, now_ms: int) -> list[dict[str, Any]]:
        self.last_now_ms = now_ms
        messages: list[dict[str, Any]] = []
        if self.pending is not None and now_ms - self.pending.start_ms >= self.pending.duration_ms:
            action = self.pending
            self.cell = action.target_cell
            self.heading_index = action.target_heading
            if action.name == "move_cell":
                self.enc_left += action.target_ticks
                self.enc_right += action.target_ticks
            elif action.name == "turn_left":
                self.enc_left -= action.target_ticks
                self.enc_right += action.target_ticks
            elif action.name == "turn_right":
                self.enc_left += action.target_ticks
                self.enc_right -= action.target_ticks
            else:
                self.enc_left += action.target_ticks
                self.enc_right -= action.target_ticks
            self.pending = None
            self.state = "IDLE"
            messages.append(
                {
                    "type": "done",
                    "action_id": action.action_id,
                    "name": action.name,
                    "success": True,
                    "duration_ms": action.duration_ms,
                    "enc_left": self.enc_left,
                    "enc_right": self.enc_right,
                    "simulated": True,
                }
            )

        if now_ms - self.last_telemetry_ms >= 100:
            self.last_telemetry_ms = now_ms
            messages.append(self.telemetry_message())
        return messages

    def telemetry_message(self) -> dict[str, Any]:
        front_mm, left_mm, right_mm = self._sensor_distances()
        return {
            "type": "telemetry",
            "state": self.state,
            "front_mm": front_mm,
            "left_mm": left_mm,
            "right_mm": right_mm,
            "enc_left": self.enc_left,
            "enc_right": self.enc_right,
            "pwm_left": 0 if self.pending is None else 80,
            "pwm_right": 0 if self.pending is None else 80,
            "param_version": self.param_version,
            "simulated": True,
            "sim_cell": list(self.cell),
            "sim_heading": self.heading,
        }

    def world_pose(self) -> tuple[float, float, float]:
        """Return interpolated Webots x, z and yaw values."""
        if self.pending is None:
            x, z = self._cell_to_world(self.cell)
            return x, z, HEADING_ANGLES[self.heading]

        action = self.pending
        progress = min(1.0, max(0.0, (self.last_now_ms - action.start_ms) / action.duration_ms))
        eased = progress * progress * (3.0 - 2.0 * progress)
        start_x, start_z = self._cell_to_world(action.start_cell)
        target_x, target_z = self._cell_to_world(action.target_cell)
        x = start_x + (target_x - start_x) * eased
        z = start_z + (target_z - start_z) * eased
        yaw = HEADING_ANGLES[HEADINGS[action.start_heading]] + action.angle_delta * eased
        return x, z, yaw

    def _start_action(self, message: Mapping[str, Any], *, seq: int, now_ms: int) -> list[dict[str, Any]]:
        action_id = str(message.get("action_id") or "")
        name = str(message.get("name") or "")
        if not action_id:
            return [self._ack(seq, ok=False, message="action_id is required")]
        if name not in {"move_cell", "turn_left", "turn_right", "turn_back"}:
            return [self._ack(seq, ok=False, message=f"unsupported action: {name}")]
        if self.estopped:
            return [self._ack(seq, ok=False, message="simulation is in ESTOP")]
        if self.state == "PAUSED":
            return [self._ack(seq, ok=False, message="simulation is paused")]
        if self.pending is not None:
            return [self._ack(seq, ok=False, message="another action is active")]

        target_ticks = max(1, int(message.get("target_ticks") or 1))
        target_cell = self.cell
        target_heading = self.heading_index
        angle_delta = 0.0
        duration_ms = 700

        if name == "move_cell":
            if self._has_wall(self.cell, self.heading):
                return [
                    self._ack(seq),
                    {
                        "type": "error",
                        "action_id": action_id,
                        "name": name,
                        "code": "OBSTACLE_TOO_CLOSE",
                        "message": "simulated wall blocks the next cell",
                        "front_mm": 80,
                        "simulated": True,
                    },
                ]
            dx, dy = DELTAS[self.heading]
            target_cell = (self.cell[0] + dx, self.cell[1] + dy)
            self.state = "MOVING_CELL"
        elif name == "turn_left":
            target_heading = (self.heading_index - 1) % 4
            angle_delta = math.pi / 2
            duration_ms = 420
            self.state = "TURNING_LEFT"
        elif name == "turn_right":
            target_heading = (self.heading_index + 1) % 4
            angle_delta = -math.pi / 2
            duration_ms = 420
            self.state = "TURNING_RIGHT"
        else:
            target_heading = (self.heading_index + 2) % 4
            angle_delta = math.pi
            duration_ms = 650
            self.state = "TURNING_BACK"

        self.pending = PendingAction(
            action_id=action_id,
            name=name,
            target_ticks=target_ticks,
            start_ms=now_ms,
            duration_ms=duration_ms,
            start_cell=self.cell,
            target_cell=target_cell,
            start_heading=self.heading_index,
            target_heading=target_heading,
            angle_delta=angle_delta,
        )
        return [self._ack(seq), self.telemetry_message()]

    def _cancel_pending(self, code: str, message: str) -> list[dict[str, Any]]:
        if self.pending is None:
            return []
        action = self.pending
        self.pending = None
        return [
            {
                "type": "error",
                "action_id": action.action_id,
                "name": action.name,
                "code": code,
                "message": message,
                "simulated": True,
            }
        ]

    def _sensor_distances(self) -> tuple[int, int, int]:
        headings = (
            self.heading,
            HEADINGS[(self.heading_index - 1) % 4],
            HEADINGS[(self.heading_index + 1) % 4],
        )
        return tuple(80 if self._has_wall(self.cell, heading) else 500 for heading in headings)

    def _has_wall(self, cell: Cell, heading: str) -> bool:
        dx, dy = DELTAS[heading]
        neighbor = (cell[0] + dx, cell[1] + dy)
        if not (0 <= neighbor[0] < self.width and 0 <= neighbor[1] < self.height):
            return True
        return _edge(cell, neighbor) in INTERNAL_WALLS

    def _cell_to_world(self, cell: Cell) -> tuple[float, float]:
        x = (cell[0] - (self.width - 1) / 2) * self.cell_size_m
        z = (cell[1] - (self.height - 1) / 2) * self.cell_size_m
        return x, z

    @staticmethod
    def _ack(seq: int, *, ok: bool = True, message: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": "ack", "seq": seq, "ok": ok}
        if message:
            payload["message"] = message
        return payload
