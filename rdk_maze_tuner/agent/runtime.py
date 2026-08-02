"""RDK-local map-goal navigation, evidence gating and safe termination."""

from __future__ import annotations

import hashlib
import json
import threading
from enum import Enum
from typing import Any, Callable, Mapping

from rdk_maze_tuner.core.device_session import (
    DeviceDisconnectedError,
)
from rdk_maze_tuner.core.goal_directed_planner import GoalDirectedPlanner
from rdk_maze_tuner.core.goal_verifier import GoalVerifier
from rdk_maze_tuner.core.maze_map import MazeMap
from rdk_maze_tuner.core.maze_runner import MazeRunner
from rdk_maze_tuner.core.motion_evidence import (
    ArrivalVerificationConfig,
)
from rdk_maze_tuner.core.serial_client import SerialClientError
from rdk_maze_tuner.core.task_pose_tracker import TaskPoseTracker
from rdk_maze_tuner.core.maze_validation import validate_map_definition


class AgentEnvelopeError(ValueError):
    """Raised when a server task envelope is not self-consistent."""


class AgentRuntimeState(str, Enum):
    IDLE = "IDLE"
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    LOST = "LOST"
    ERROR = "ERROR"
    ESTOP = "ESTOP"


class SnapshotParams:
    """Read-only ParamManager-compatible view of an immutable snapshot."""

    def __init__(self, snapshot: Mapping[str, Any]) -> None:
        self.params = _clone(snapshot)

    def get(self, dotted_path: str) -> Any:
        node: Any = self.params
        for part in dotted_path.split("."):
            if not isinstance(node, Mapping) or part not in node:
                raise KeyError(dotted_path)
            node = node[part]
        return node

    def arrival_verification_config(
        self,
    ) -> ArrivalVerificationConfig:
        value = self.params.get("arrival_verification")
        return ArrivalVerificationConfig.from_mapping(
            value if isinstance(value, Mapping) else None
        )


def build_task_envelope(
    *,
    task_id: str,
    run_id: str,
    map_version_id: str,
    map_digest: str,
    map_definition: Mapping[str, Any],
    goal: Mapping[str, Any],
    param_version_id: str,
    param_digest: str,
    param_snapshot: Mapping[str, Any],
    arrival_verification: Mapping[str, Any],
    max_steps: int,
) -> dict[str, Any]:
    completion = ArrivalVerificationConfig.from_mapping(
        arrival_verification
    ).to_dict()
    payload = {
        "type": "task.prepare",
        "schema_version": 1,
        "task_id": str(task_id),
        "run_id": str(run_id),
        "map": {
            "version_id": str(map_version_id),
            "digest": str(map_digest),
            "definition": _clone(map_definition),
        },
        "goal": _clone(goal),
        "params": {
            "version_id": str(param_version_id),
            "digest": str(param_digest),
            "snapshot": _clone(param_snapshot),
        },
        "completion": completion,
        "max_steps": int(max_steps),
    }
    return validate_task_envelope(payload)


def validate_task_envelope(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise AgentEnvelopeError("task envelope must be an object")
    normalized = _clone(payload)
    if normalized.get("type") != "task.prepare":
        raise AgentEnvelopeError("task envelope type must be task.prepare")
    for field in ("task_id", "run_id"):
        if not str(normalized.get(field) or "").strip():
            raise AgentEnvelopeError(f"{field} is required")
    map_payload = normalized.get("map")
    goal = normalized.get("goal")
    params = normalized.get("params")
    if not isinstance(map_payload, Mapping):
        raise AgentEnvelopeError("map identity is required")
    if not isinstance(goal, Mapping):
        raise AgentEnvelopeError("primary goal is required")
    if not isinstance(params, Mapping):
        raise AgentEnvelopeError("parameter identity is required")
    try:
        definition = validate_map_definition(
            map_payload.get("definition")
        )
    except (TypeError, ValueError) as exc:
        raise AgentEnvelopeError(f"invalid map definition: {exc}") from exc
    version_id = str(map_payload.get("version_id") or "")
    digest = str(map_payload.get("digest") or "")
    if not version_id or digest != definition.content_digest:
        raise AgentEnvelopeError("map version or digest mismatch")
    cell = goal.get("cell")
    expected_goal = (
        [definition.goals[0][0], definition.goals[0][1]]
        if definition.goals
        else None
    )
    if (
        goal.get("type") != "map_goal"
        or cell != expected_goal
        or goal.get("source_map_version") != version_id
        or goal.get("source_map_digest") != digest
    ):
        raise AgentEnvelopeError(
            "task goal does not match the map primary goal"
        )
    param_snapshot = params.get("snapshot")
    if (
        not str(params.get("version_id") or "")
        or len(str(params.get("digest") or "")) != 64
        or not isinstance(param_snapshot, Mapping)
    ):
        raise AgentEnvelopeError("invalid parameter version identity")
    try:
        completion = ArrivalVerificationConfig.from_mapping(
            normalized.get("completion")
        )
    except (TypeError, ValueError) as exc:
        raise AgentEnvelopeError(
            f"invalid completion thresholds: {exc}"
        ) from exc
    max_steps = normalized.get("max_steps")
    if (
        not isinstance(max_steps, int)
        or isinstance(max_steps, bool)
        or not 1 <= max_steps <= 10_000
    ):
        raise AgentEnvelopeError("max_steps must be between 1 and 10000")
    normalized["completion"] = completion.to_dict()
    return normalized


class AgentRuntime:
    """Own all per-action control on RDK; the server sees task events only."""

    def __init__(
        self,
        *,
        session,
        event_sink: Callable[[dict[str, Any]], None],
    ) -> None:
        self.session = session
        self.event_sink = event_sink
        self.state = AgentRuntimeState.IDLE
        self.envelope: dict[str, Any] | None = None
        self.params: SnapshotParams | None = None
        self.maze: MazeMap | None = None
        self.planner: GoalDirectedPlanner | None = None
        self.pose_tracker: TaskPoseTracker | None = None
        self.goal_verifier: GoalVerifier | None = None
        self.runner: MazeRunner | None = None
        self._stop_requested = threading.Event()

    def prepare(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self.state is AgentRuntimeState.RUNNING:
            raise RuntimeError("cannot prepare while a task is running")
        envelope = validate_task_envelope(payload)
        definition = validate_map_definition(
            envelope["map"]["definition"]
        )
        params = SnapshotParams(envelope["params"]["snapshot"])
        completion = ArrivalVerificationConfig.from_mapping(
            envelope["completion"]
        )
        maze = MazeMap.from_definition(
            definition,
            wall_threshold_mm=int(
                params.get("tof.wall_threshold_mm")
            ),
            map_version_id=envelope["map"]["version_id"],
        )
        planner = GoalDirectedPlanner(
            goal_cells=(tuple(envelope["goal"]["cell"]),)
        )
        tracker = TaskPoseTracker.from_params(
            maze=maze,
            params=params,
            arrival_config=completion,
            run_id=envelope["run_id"],
        )
        verifier = GoalVerifier(
            goal=envelope["goal"],
            map_version_id=maze.map_version_id,
            map_digest=maze.map_digest,
            cell_width_mm=float(maze.cell_width_mm),
            cell_height_mm=float(maze.cell_height_mm),
            config=completion,
        )
        self.session.start()
        ready = self.session.wait_ready(timeout_s=3.0)
        self.envelope = envelope
        self.params = params
        self.maze = maze
        self.planner = planner
        self.pose_tracker = tracker
        self.goal_verifier = verifier
        self.runner = MazeRunner(
            client=self.session,
            params=params,
            maze=maze,
            planner=planner,
            action_prefix=envelope["run_id"],
            pose_tracker=tracker,
            goal_verifier=verifier,
            enable_recovery=True,
        )
        self._stop_requested.clear()
        self.state = AgentRuntimeState.READY
        prepared = {
            "task_id": envelope["task_id"],
            "run_id": envelope["run_id"],
            "status": self.state.value,
            "map": {
                "version_id": maze.map_version_id,
                "digest": maze.map_digest,
            },
            "goal": _clone(envelope["goal"]),
            "ready": _clone(ready),
        }
        self._emit("task.status", prepared)
        return prepared

    def run(self) -> dict[str, Any]:
        if self.state is AgentRuntimeState.LOST:
            raise RuntimeError("task is LOST and cannot automatically resume")
        if self.state is not AgentRuntimeState.READY:
            raise RuntimeError(
                f"run requires READY, got {self.state.value}"
            )
        assert self.envelope is not None
        assert self.runner is not None
        self.state = AgentRuntimeState.RUNNING
        self._emit_status()
        try:
            for _step in range(self.envelope["max_steps"]):
                if self._stop_requested.is_set():
                    return self._terminal(
                        AgentRuntimeState.LOST,
                        result="stopped",
                        reason="local stop requested",
                    )
                result = self.runner.run_step(
                    control=self,
                    goal=lambda maze, _telemetry: (
                        list(maze.position)
                        == self.envelope["goal"]["cell"]
                    ),
                    event_sink=self._forward_runner_event,
                )
                if result.outcome == "goal_verified":
                    self._safe_stop()
                    return self._terminal(
                        AgentRuntimeState.COMPLETED,
                        result="goal_verified",
                        pose=result.reliable_pose,
                        verification=result.goal_verification,
                    )
                if result.outcome in {
                    "unsafe",
                    "recovery_required",
                    "exhausted",
                }:
                    self._safe_stop()
                    return self._terminal(
                        AgentRuntimeState.ERROR,
                        result=result.outcome,
                        reason=result.error_code or "navigation failed",
                        pose=result.reliable_pose,
                    )
            self._safe_stop()
            return self._terminal(
                AgentRuntimeState.ERROR,
                result="max_steps",
                reason="task exceeded max_steps",
            )
        except DeviceDisconnectedError as exc:
            self._safe_stop()
            return self._terminal(
                AgentRuntimeState.LOST,
                result="serial_disconnected",
                reason=str(exc),
            )
        except SerialClientError as exc:
            self._safe_stop()
            return self._terminal(
                AgentRuntimeState.ERROR,
                result="device_error",
                reason=str(exc),
            )

    def pause_requested(self) -> bool:
        return False

    def stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    def on_cloud_disconnect(self) -> dict[str, Any]:
        self._stop_requested.set()
        stop = self._safe_stop()
        return self._terminal(
            AgentRuntimeState.LOST,
            result="cloud_disconnected",
            reason="server connection lost; automatic resume disabled",
            stop=stop,
        )

    def stop(self, *, reason: str = "server") -> dict[str, Any]:
        if self.state is AgentRuntimeState.COMPLETED:
            return {
                "task_id": (
                    None
                    if self.envelope is None
                    else self.envelope["task_id"]
                ),
                "run_id": (
                    None
                    if self.envelope is None
                    else self.envelope["run_id"]
                ),
                "status": self.state.value,
                "result": "already_completed",
            }
        self._stop_requested.set()
        stop = self._safe_stop()
        return self._terminal(
            AgentRuntimeState.LOST,
            result="stopped",
            reason=reason,
            stop=stop,
        )

    def estop(self, *, reason: str) -> dict[str, Any]:
        self._stop_requested.set()
        try:
            acknowledgement = self.session.estop(reason=reason)
        except Exception as exc:
            acknowledgement = {"ok": False, "error": str(exc)}
        return self._terminal(
            AgentRuntimeState.ESTOP,
            result="estop",
            reason=reason,
            stop=acknowledgement,
        )

    def clear_estop(self) -> dict[str, Any]:
        if self.state is not AgentRuntimeState.ESTOP:
            raise RuntimeError("clear_estop requires ESTOP")
        request_ack = getattr(self.session, "request_ack", None)
        if not callable(request_ack):
            raise RuntimeError(
                "device session does not support clear_estop"
            )
        acknowledgement = request_ack("clear_estop")
        self.state = AgentRuntimeState.IDLE
        payload = {
            "task_id": (
                None if self.envelope is None else self.envelope["task_id"]
            ),
            "run_id": (
                None if self.envelope is None else self.envelope["run_id"]
            ),
            "status": self.state.value,
            "ack": _clone(acknowledgement),
        }
        self._emit("task.status", payload)
        return payload

    def close(self) -> None:
        self._stop_requested.set()
        self._safe_stop()
        self.session.close()

    def _safe_stop(self) -> dict[str, Any]:
        try:
            acknowledgement = self.session.stop()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        try:
            telemetry = self.session.wait_telemetry(timeout_s=1.0)
        except Exception:
            telemetry = None
        return {
            "ok": True,
            "ack": _clone(acknowledgement),
            "telemetry": (
                None if telemetry is None else _clone(telemetry)
            ),
        }

    def _forward_runner_event(self, event: Mapping[str, Any]) -> None:
        assert self.envelope is not None
        self._emit(
            "task.event",
            {
                "task_id": self.envelope["task_id"],
                "run_id": self.envelope["run_id"],
                "event": _clone(event),
            },
        )

    def _terminal(
        self,
        state: AgentRuntimeState,
        *,
        result: str,
        **details: Any,
    ) -> dict[str, Any]:
        self.state = state
        payload = {
            "task_id": (
                None if self.envelope is None else self.envelope["task_id"]
            ),
            "run_id": (
                None if self.envelope is None else self.envelope["run_id"]
            ),
            "status": state.value,
            "result": result,
            **_clone(details),
        }
        self._emit("task.result", payload)
        return payload

    def _emit_status(self) -> None:
        self._emit(
            "task.status",
            {
                "task_id": (
                    None
                    if self.envelope is None
                    else self.envelope["task_id"]
                ),
                "run_id": (
                    None
                    if self.envelope is None
                    else self.envelope["run_id"]
                ),
                "status": self.state.value,
            },
        )

    def _emit(
        self,
        message_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        self.event_sink(
            {
                "type": message_type,
                "schema_version": 1,
                "payload": _clone(payload),
            }
        )


def _clone(value: Any) -> Any:
    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def canonical_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
