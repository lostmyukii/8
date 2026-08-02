"""Authenticated task-level adapter for an outbound RDK X3 Agent."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from rdk_maze_tuner.agent.runtime import build_task_envelope
from rdk_maze_tuner.core.device_session import DeviceDisconnectedError
from rdk_maze_tuner.core.maze_map import PlannedAction
from rdk_maze_tuner.core.maze_runner import MazeStepResult
from rdk_maze_tuner.platform.agent_registry import (
    AgentConnection,
    AgentOfflineError,
    AgentRegistry,
)

from .base import ModeAdapter, ModeAdapterError


DEVICE_OFFLINE = "DEVICE_OFFLINE"
DEVICE_OFFLINE_MESSAGE = "RDK X3 Agent is offline"


class RemoteAgentTaskRunner:
    """Convert one whole Agent task result into one orchestrator step."""

    def __init__(
        self,
        *,
        connection: AgentConnection,
        task_id: str,
        run_id: str,
        poll_timeout_s: float = 0.2,
    ) -> None:
        self.connection = connection
        self.task_id = task_id
        self.run_id = run_id
        self.poll_timeout_s = poll_timeout_s
        self._index = 0

    def run_step(self, *, control, goal, event_sink) -> MazeStepResult:
        del goal
        while True:
            if control.stop_requested():
                return _remote_step("stopped")
            batch = self.connection.updates(
                self._index,
                timeout_s=self.poll_timeout_s,
            )
            self._index = batch.next_index
            for message in batch.messages:
                if not _message_matches(
                    message,
                    task_id=self.task_id,
                    run_id=self.run_id,
                ):
                    continue
                message_type = message["type"]
                payload = message.get("payload")
                if message_type == "task.event":
                    event = (
                        payload.get("event")
                        if isinstance(payload, Mapping)
                        else None
                    )
                    if isinstance(event, Mapping):
                        event_sink(dict(event))
                elif message_type == "task.status":
                    event_sink(
                        {
                            "type": "agent.task_status",
                            "payload": dict(payload or {}),
                        }
                    )
                elif message_type == "task.result":
                    return _terminal_step(payload)
            if not batch.connected:
                raise DeviceDisconnectedError(
                    "authenticated RDK Agent connection lost"
                )


class RealModeAdapter(ModeAdapter):
    mode = "real"

    def __init__(
        self,
        *,
        registry: AgentRegistry | None = None,
        device_id: str | None = None,
        map_provider: Callable[[str], Any] | None = None,
        param_provider: Callable[[str], Any] | None = None,
    ) -> None:
        self.registry = registry or AgentRegistry()
        self.device_id = device_id
        self.map_provider = map_provider
        self.param_provider = param_provider
        self._connection: AgentConnection | None = None
        self._prepared: dict[str, Any] | None = None
        self._loaded_identity: dict[str, str] | None = None

    def preflight(
        self,
        *,
        map_version: str | None = None,
        param_version: str | None = None,
        physical_profile_id: str | None = None,
    ) -> dict[str, Any]:
        if physical_profile_id:
            raise ModeAdapterError(
                "PHYSICAL_PROFILE_NOT_APPLICABLE",
                "Webots physical profiles cannot be used in real mode",
            )
        try:
            connection = self.registry.require_online(self.device_id)
        except AgentOfflineError:
            return {
                "ok": False,
                "mode": self.mode,
                "code": DEVICE_OFFLINE,
                "message": DEVICE_OFFLINE_MESSAGE,
            }
        self._connection = connection
        return {
            "ok": True,
            "mode": self.mode,
            "code": "READY",
            "device": connection.snapshot(),
            "controller_version": "rdk-agent",
            "webots_version": "not-applicable",
            "map_version": map_version,
            "param_version": param_version,
        }

    def reset(
        self,
        *,
        map_version: str,
        param_version: str,
        physical_profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if physical_profile is not None:
            raise ModeAdapterError(
                "PHYSICAL_PROFILE_NOT_APPLICABLE",
                "Webots physical profiles cannot be used in real mode",
            )
        connection = self._require_connection()
        self._loaded_identity = {
            "map_version": str(map_version),
            "param_version": str(param_version),
        }
        self._prepared = None
        return {
            "ok": True,
            "mode": self.mode,
            "command": "reset",
            "device_id": connection.principal.device_id,
            **self._loaded_identity,
        }

    def prepare_task(self, task) -> RemoteAgentTaskRunner:
        connection = self._require_connection()
        if self.map_provider is None or self.param_provider is None:
            raise ModeAdapterError(
                "AGENT_ASSETS_UNAVAILABLE",
                "map and parameter providers are required for real mode",
            )
        map_version = self.map_provider(task.map_version)
        param_version = self.param_provider(task.param_version)
        map_payload = (
            map_version.to_dict()
            if hasattr(map_version, "to_dict")
            else dict(map_version)
        )
        param_payload = (
            param_version.to_dict()
            if hasattr(param_version, "to_dict")
            else dict(param_version)
        )
        envelope = build_task_envelope(
            task_id=task.task_id,
            run_id=task.run_id,
            map_version_id=str(
                map_payload.get("version_id") or task.map_version
            ),
            map_digest=str(map_payload["digest"]),
            map_definition=_definition_payload(
                map_payload["definition"]
            ),
            goal=task.goal,
            param_version_id=str(
                param_payload.get("version_id") or task.param_version
            ),
            param_digest=str(param_payload["digest"]),
            param_snapshot=param_payload["snapshot"],
            arrival_verification=(
                task.arrival_verification_snapshot
            ),
            max_steps=task.max_steps,
        )
        connection.send(envelope)
        self._prepared = envelope
        return RemoteAgentTaskRunner(
            connection=connection,
            task_id=task.task_id,
            run_id=task.run_id,
        )

    def start(self) -> dict[str, Any]:
        self._require_connection()
        envelope = self._require_prepared()
        self._send(
            {
                "type": "task.start",
                "schema_version": 1,
                "task_id": envelope["task_id"],
                "run_id": envelope["run_id"],
            }
        )
        return self._result("start")

    def pause(self) -> dict[str, Any]:
        self._send_for_task("task.pause")
        return self._result("pause")

    def stop(self) -> dict[str, Any]:
        self._send_for_task("task.stop")
        return self._result("stop")

    def estop(self) -> dict[str, Any]:
        self._send_for_task("task.estop")
        return self._result("estop")

    def clear_estop(self) -> dict[str, Any]:
        self._send_for_task("task.clear_estop")
        return self._result("clear_estop")

    def snapshot(self) -> dict[str, Any]:
        agent = self.registry.snapshot(self.device_id)
        if agent.get("connected") is not True:
            agent = {
                **agent,
                "status": DEVICE_OFFLINE,
                "code": DEVICE_OFFLINE,
                "message": DEVICE_OFFLINE_MESSAGE,
            }
        return {
            "mode": self.mode,
            **agent,
            "loaded_identity": (
                None
                if self._loaded_identity is None
                else dict(self._loaded_identity)
            ),
            "prepared_task": (
                None
                if self._prepared is None
                else {
                    "task_id": self._prepared["task_id"],
                    "run_id": self._prepared["run_id"],
                    "map_version": self._prepared["map"][
                        "version_id"
                    ],
                    "param_version": self._prepared["params"][
                        "version_id"
                    ],
                }
            ),
        }

    def close(self) -> None:
        self._connection = None

    def _send_for_task(self, message_type: str) -> None:
        self._require_connection()
        envelope = self._require_prepared()
        self._send(
            {
                "type": message_type,
                "schema_version": 1,
                "task_id": envelope["task_id"],
                "run_id": envelope["run_id"],
            }
        )

    def _send(self, message: Mapping[str, Any]) -> None:
        self._require_connection().send(message)

    def _require_connection(self) -> AgentConnection:
        try:
            connection = self.registry.require_online(self.device_id)
        except AgentOfflineError as exc:
            raise ModeAdapterError(
                DEVICE_OFFLINE,
                DEVICE_OFFLINE_MESSAGE,
            ) from exc
        self._connection = connection
        return connection

    def _require_prepared(self) -> dict[str, Any]:
        if self._prepared is None:
            raise ModeAdapterError(
                "TASK_NOT_PREPARED",
                "real task must be prepared before control commands",
            )
        return self._prepared

    def _result(self, command: str) -> dict[str, Any]:
        connection = self._require_connection()
        return {
            "ok": True,
            "mode": self.mode,
            "command": command,
            "device_id": connection.principal.device_id,
        }


def _definition_payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, Mapping):
        return dict(value)
    raise ModeAdapterError(
        "MAP_DEFINITION_INVALID",
        "map definition cannot be serialized",
    )


def _message_matches(
    message: Mapping[str, Any],
    *,
    task_id: str,
    run_id: str,
) -> bool:
    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        return False
    return (
        payload.get("task_id") == task_id
        and payload.get("run_id") == run_id
    )


def _remote_step(outcome: str) -> MazeStepResult:
    return MazeStepResult(
        action=PlannedAction("stop"),
        action_id=None,
        telemetry={},
        done=None,
        map_text="",
        outcome=outcome,
    )


def _terminal_step(value: Any) -> MazeStepResult:
    payload = dict(value) if isinstance(value, Mapping) else {}
    status = str(payload.get("status") or "")
    if status == "LOST":
        raise DeviceDisconnectedError(
            str(payload.get("reason") or "RDK Agent reported LOST")
        )
    if status == "COMPLETED" and payload.get("result") == (
        "goal_verified"
    ):
        return MazeStepResult(
            action=PlannedAction("stop"),
            action_id=None,
            telemetry={"source": "rdk-agent"},
            done=None,
            map_text="",
            outcome="goal_verified",
            reliable_pose=payload.get("pose"),
            goal_verification=payload.get("verification"),
        )
    return MazeStepResult(
        action=PlannedAction("stop"),
        action_id=None,
        telemetry={"source": "rdk-agent"},
        done=None,
        map_text="",
        outcome="unsafe",
        reliable_pose=payload.get("pose"),
        error_code=str(
            payload.get("reason")
            or payload.get("result")
            or "AGENT_TASK_FAILED"
        ),
    )
