"""Deterministic action-level model of the ESP32 maze-car contract."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from rdk_maze_tuner.core.maze_definition import MapDefinition
from rdk_maze_tuner.core.maze_validation import (
    MazeValidationError,
    validate_map_definition,
)
from simulation.webots.maze_car.map_loader import CompiledMap, compile_map


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

    def __init__(self) -> None:
        self.params: dict[str, Any] = {}
        self.param_version = 1
        self.map_definition = _default_map_definition()
        self.compiled_map = compile_map(self.map_definition)
        self.map_version_id = "builtin-open-5x5"
        self.map_digest = self.map_definition.content_digest
        self.map_revision = 1
        self._apply_compiled_map(self.compiled_map)
        self._reset_state()

    def _reset_state(self) -> None:
        self.cell = self.compiled_map.start_cell
        self.heading_index = HEADINGS.index(
            self.compiled_map.start_heading
        )
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
            "imu_available": True,
            "features": [
                "motor",
                "encoder",
                "tof",
                "imu_simulated",
                "json_serial",
            ],
            "simulated": True,
            "map_version_id": self.map_version_id,
            "map_digest": self.map_digest,
        }

    def on_client_connected(self, *, now_ms: int) -> None:
        """Deterministic backend has no connection-scoped state."""

    def on_client_disconnected(self, *, now_ms: int) -> None:
        """Deterministic backend has no motors to stop."""

    def close(self) -> None:
        """Deterministic backend owns no external resources."""

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
        if message_type == "load_map":
            return self._load_map(message, seq=seq)
        if message_type == "reset":
            mismatch = self._map_mismatch(message)
            if mismatch is not None:
                return [self._ack(seq, ok=False, message=mismatch)]
            self._reset_state()
            return [
                self._map_ack(seq),
                self.telemetry_message(),
            ]
        if message_type == "start":
            if self.estopped:
                return [
                    self._ack(
                        seq,
                        ok=False,
                        message="simulation is in ESTOP",
                    )
                ]
            mismatch = self._map_mismatch(message)
            if mismatch is not None:
                return [self._ack(seq, ok=False, message=mismatch)]
            self.state = "IDLE"
            return [self._map_ack(seq), self.telemetry_message()]
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
        fusion_front_mm, fusion_left_mm, fusion_right_mm = (
            self._continuous_wall_distances()
        )
        enc_left, enc_right = self._current_encoder_counts()
        truth = self._truth_pose()
        imu_yaw = normalize_navigation_yaw(
            truth["yaw_deg"]
            + 0.35 * math.sin(self.last_now_ms / 1000.0)
        )
        return {
            "type": "telemetry",
            "ts_ms": self.last_now_ms,
            "state": self.state,
            "front_mm": front_mm,
            "left_mm": left_mm,
            "right_mm": right_mm,
            "fusion_front_mm": fusion_front_mm,
            "fusion_left_mm": fusion_left_mm,
            "fusion_right_mm": fusion_right_mm,
            "enc_left": enc_left,
            "enc_right": enc_right,
            "pwm_left": 0 if self.pending is None else 80,
            "pwm_right": 0 if self.pending is None else 80,
            "param_version": self.param_version,
            "imu_available": True,
            "imu_quality": "simulated",
            "imu_yaw_deg": float(round(imu_yaw, 4)),
            "yaw_rate_dps": float(
                round(self._imu_yaw_rate_dps(), 4)
            ),
            "accel_forward_mps2": 0.0,
            "quality_flags": [],
            "simulated": True,
            "sim_cell": list(self.cell),
            "sim_heading": self.heading,
            "sim_truth": truth,
            "map_version_id": self.map_version_id,
            "map_digest": self.map_digest,
        }

    def world_pose(self) -> tuple[float, float, float]:
        """Return interpolated Webots x, z and yaw values."""
        if self.pending is None:
            x, z = self._cell_to_world(self.cell)
            return x, z, HEADING_ANGLES[self.heading]

        action = self.pending
        eased = self._motion_progress()
        start_x, start_z = self._cell_to_world(action.start_cell)
        target_x, target_z = self._cell_to_world(action.target_cell)
        x = start_x + (target_x - start_x) * eased
        z = start_z + (target_z - start_z) * eased
        yaw = HEADING_ANGLES[HEADINGS[action.start_heading]] + action.angle_delta * eased
        return x, z, yaw

    def _truth_pose(self) -> dict[str, Any]:
        if self.pending is None:
            grid_x = float(self.cell[0])
            grid_y = float(self.cell[1])
            yaw_deg = heading_navigation_yaw(self.heading)
        else:
            action = self.pending
            progress = self._motion_progress()
            grid_x = (
                action.start_cell[0]
                + (action.target_cell[0] - action.start_cell[0])
                * progress
            )
            grid_y = (
                action.start_cell[1]
                + (action.target_cell[1] - action.start_cell[1])
                * progress
            )
            start_yaw = heading_navigation_yaw(
                HEADINGS[action.start_heading]
            )
            yaw_deg = normalize_navigation_yaw(
                start_yaw
                - math.degrees(action.angle_delta) * progress
            )
        return {
            "x_mm": round(
                (grid_x + 0.5) * self.cell_width_m * 1000.0,
                3,
            ),
            "y_mm": round(
                (grid_y + 0.5) * self.cell_height_m * 1000.0,
                3,
            ),
            "yaw_deg": round(yaw_deg, 3),
            "cell": list(self.cell),
            "heading": self.heading,
        }

    def _motion_progress(self) -> float:
        if self.pending is None:
            return 0.0
        action = self.pending
        progress = min(
            1.0,
            max(
                0.0,
                (self.last_now_ms - action.start_ms)
                / action.duration_ms,
            ),
        )
        return progress * progress * (3.0 - 2.0 * progress)

    def _current_encoder_counts(self) -> tuple[int, int]:
        if self.pending is None:
            return self.enc_left, self.enc_right
        ticks = int(
            round(self.pending.target_ticks * self._motion_progress())
        )
        if self.pending.name == "move_cell":
            return self.enc_left + ticks, self.enc_right + ticks
        if self.pending.name == "turn_left":
            return self.enc_left - ticks, self.enc_right + ticks
        return self.enc_left + ticks, self.enc_right - ticks

    def _imu_yaw_rate_dps(self) -> float:
        if self.pending is None:
            return 0.0
        return (
            -math.degrees(self.pending.angle_delta)
            / (self.pending.duration_ms / 1000.0)
        )

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

    def _continuous_wall_distances(self) -> tuple[int, int, int]:
        truth = self._truth_pose()
        headings = (
            self.heading,
            HEADINGS[(self.heading_index - 1) % 4],
            HEADINGS[(self.heading_index + 1) % 4],
        )
        return tuple(
            self._distance_to_wall_mm(
                x_mm=float(truth["x_mm"]),
                y_mm=float(truth["y_mm"]),
                heading=heading,
            )
            for heading in headings
        )

    def _distance_to_wall_mm(
        self,
        *,
        x_mm: float,
        y_mm: float,
        heading: str,
    ) -> int:
        width_mm = self.cell_width_m * 1000.0
        height_mm = self.cell_height_m * 1000.0
        cell_x = min(
            self.width - 1,
            max(0, int(x_mm // width_mm)),
        )
        cell_y = min(
            self.height - 1,
            max(0, int(y_mm // height_mm)),
        )
        cell = (cell_x, cell_y)
        if heading == "N":
            distance = y_mm - cell_y * height_mm
            step = height_mm
        elif heading == "S":
            distance = (cell_y + 1) * height_mm - y_mm
            step = height_mm
        elif heading == "W":
            distance = x_mm - cell_x * width_mm
            step = width_mm
        else:
            distance = (cell_x + 1) * width_mm - x_mm
            step = width_mm

        while not self._has_wall(cell, heading):
            dx, dy = DELTAS[heading]
            cell = (cell[0] + dx, cell[1] + dy)
            distance += step
        return max(0, int(round(distance)))

    def _has_wall(self, cell: Cell, heading: str) -> bool:
        dx, dy = DELTAS[heading]
        neighbor = (cell[0] + dx, cell[1] + dy)
        if not (0 <= neighbor[0] < self.width and 0 <= neighbor[1] < self.height):
            return True
        return _edge(cell, neighbor) in self.internal_walls

    def _cell_to_world(self, cell: Cell) -> tuple[float, float]:
        x = (
            cell[0] - (self.width - 1) / 2
        ) * self.cell_width_m
        z = (
            cell[1] - (self.height - 1) / 2
        ) * self.cell_height_m
        return x, z

    def _load_map(
        self,
        message: Mapping[str, Any],
        *,
        seq: int,
    ) -> list[dict[str, Any]]:
        if self.pending is not None or self.state not in {"IDLE", "PAUSED"}:
            return [
                self._ack(
                    seq,
                    ok=False,
                    message="cannot load map while an action is active",
                )
            ]
        map_version_id = str(message.get("map_version_id") or "")
        expected_digest = str(message.get("digest") or "")
        definition_payload = message.get("definition")
        if not map_version_id:
            return [
                self._ack(
                    seq,
                    ok=False,
                    message="map_version_id is required",
                )
            ]
        try:
            definition = validate_map_definition(definition_payload)
        except (MazeValidationError, TypeError) as exc:
            return [self._ack(seq, ok=False, message=str(exc))]
        if definition.content_digest != expected_digest:
            return [
                self._ack(
                    seq,
                    ok=False,
                    message="map digest does not match definition",
                )
            ]
        compiled = compile_map(definition)
        self.map_definition = definition
        self.compiled_map = compiled
        self.map_version_id = map_version_id
        self.map_digest = compiled.digest
        self.map_revision += 1
        self._apply_compiled_map(compiled)
        self._reset_state()
        return [self._map_ack(seq)]

    def _apply_compiled_map(self, compiled: CompiledMap) -> None:
        self.width = compiled.cols
        self.height = compiled.rows
        self.cell_width_m = compiled.cell_width_m
        self.cell_height_m = compiled.cell_height_m
        self.internal_walls = compiled.internal_walls

    def _map_mismatch(self, message: Mapping[str, Any]) -> str | None:
        # ``map_version`` is the pre-Task-6 compatibility field and carries no
        # digest-backed definition.  Exact map checks are intentionally tied
        # to the new ``map_version_id`` + ``digest`` pair.
        requested_version = str(message.get("map_version_id") or "")
        requested_digest = str(message.get("digest") or "")
        if requested_version and requested_version != self.map_version_id:
            return "requested map version is not loaded"
        if requested_digest and requested_digest != self.map_digest:
            return "requested map digest is not loaded"
        return None

    def _map_ack(self, seq: int) -> dict[str, Any]:
        return self._ack(
            seq,
            map_version_id=self.map_version_id,
            digest=self.map_digest,
        )

    @staticmethod
    def _ack(
        seq: int,
        *,
        ok: bool = True,
        message: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": "ack", "seq": seq, "ok": ok}
        if message:
            payload["message"] = message
        payload.update(fields)
        return payload


def _default_map_definition() -> MapDefinition:
    """Safe open fallback; production tasks replace it with a saved version."""

    return validate_map_definition(
        {
            "rows": 5,
            "cols": 5,
            "cell_width_mm": 450,
            "cell_height_mm": 450,
            "wall_thickness_mm": 40,
            "wall_height_mm": 180,
            "start": {"x": 0, "y": 4, "heading": "N"},
            "goals": [{"x": 4, "y": 0}],
            "walls": [
                {"x1": 0, "y1": 0, "x2": 5, "y2": 0},
                {"x1": 5, "y1": 0, "x2": 5, "y2": 5},
                {"x1": 5, "y1": 5, "x2": 0, "y2": 5},
                {"x1": 0, "y1": 5, "x2": 0, "y2": 0},
            ],
            "source_image_digest": None,
        }
    )


def normalize_navigation_yaw(value: float) -> float:
    return float(value) % 360.0


def heading_navigation_yaw(heading: str) -> float:
    return {
        "N": 0.0,
        "E": 90.0,
        "S": 180.0,
        "W": 270.0,
    }[heading]
