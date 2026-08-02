"""Thread-safe task-level bridge to authenticated outbound RDK Agents."""

from __future__ import annotations

import queue
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from .device_tokens import DevicePrincipal


ALLOWED_SERVER_MESSAGES = {
    "task.prepare",
    "task.start",
    "task.pause",
    "task.stop",
    "task.estop",
    "task.clear_estop",
}
ALLOWED_AGENT_MESSAGES = {
    "agent.hello",
    "agent.heartbeat",
    "task.status",
    "task.event",
    "task.result",
}
FORBIDDEN_COMMAND_FIELDS = {
    "left_pwm",
    "right_pwm",
    "pwm",
    "left_motor_pwm",
    "right_motor_pwm",
}


class AgentRegistryError(RuntimeError):
    """Base server-side Agent connection failure."""


class AgentOfflineError(AgentRegistryError):
    """Raised when no authenticated live connection exists."""


class AgentProtocolError(AgentRegistryError):
    """Raised for messages outside the task-level contract."""


@dataclass(frozen=True)
class AgentMessageBatch:
    next_index: int
    messages: tuple[dict[str, Any], ...]
    connected: bool


class AgentConnection:
    def __init__(self, principal: DevicePrincipal) -> None:
        self.principal = principal
        self._outbound: queue.Queue[dict[str, Any]] = queue.Queue(
            maxsize=64
        )
        self._condition = threading.Condition(threading.RLock())
        self._messages: list[dict[str, Any]] = []
        self._connected = True

    @property
    def connected(self) -> bool:
        with self._condition:
            return self._connected

    def send(self, message: Mapping[str, Any]) -> None:
        payload = _message(message)
        if payload["type"] not in ALLOWED_SERVER_MESSAGES:
            raise AgentProtocolError(
                f"server message type is not task-level: "
                f"{payload['type']}"
            )
        forbidden = _find_forbidden(payload)
        if forbidden is not None:
            raise AgentProtocolError(
                f"server cannot send low-level motor field: {forbidden}"
            )
        payload.setdefault("message_id", f"server-{uuid.uuid4()}")
        with self._condition:
            if not self._connected:
                raise AgentOfflineError(
                    f"Agent is offline: {self.principal.device_id}"
                )
        try:
            self._outbound.put_nowait(payload)
        except queue.Full as exc:
            raise AgentRegistryError(
                "Agent command queue is full"
            ) from exc

    def next_outbound(
        self,
        *,
        timeout_s: float = 0.0,
    ) -> dict[str, Any] | None:
        try:
            return self._outbound.get(timeout=max(0.0, timeout_s))
        except queue.Empty:
            return None

    def receive(self, message: Mapping[str, Any]) -> None:
        payload = _message(message)
        if payload["type"] not in ALLOWED_AGENT_MESSAGES:
            raise AgentProtocolError(
                f"unsupported Agent message: {payload['type']}"
            )
        with self._condition:
            if not self._connected:
                return
            self._messages.append(payload)
            self._condition.notify_all()

    def updates(
        self,
        after_index: int,
        *,
        timeout_s: float,
    ) -> AgentMessageBatch:
        with self._condition:
            if (
                after_index >= len(self._messages)
                and self._connected
                and timeout_s > 0
            ):
                self._condition.wait(timeout_s)
            messages = tuple(
                dict(item)
                for item in self._messages[after_index:]
            )
            return AgentMessageBatch(
                next_index=len(self._messages),
                messages=messages,
                connected=self._connected,
            )

    def disconnect(self) -> None:
        with self._condition:
            self._connected = False
            self._condition.notify_all()

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            return {
                "device_id": self.principal.device_id,
                "name": self.principal.name,
                "connected": self._connected,
                "status": "ONLINE" if self._connected else "OFFLINE",
                "received_messages": len(self._messages),
                "queued_commands": self._outbound.qsize(),
            }


class AgentRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._connections: dict[str, AgentConnection] = {}

    def connect(self, principal: DevicePrincipal) -> AgentConnection:
        with self._lock:
            previous = self._connections.get(principal.device_id)
            if previous is not None:
                previous.disconnect()
            connection = AgentConnection(principal)
            self._connections[principal.device_id] = connection
            return connection

    def disconnect(
        self,
        device_id: str,
        connection: AgentConnection,
    ) -> None:
        with self._lock:
            current = self._connections.get(device_id)
            connection.disconnect()
            if current is connection:
                self._connections[device_id] = connection

    def require_online(
        self,
        device_id: str | None = None,
    ) -> AgentConnection:
        with self._lock:
            if device_id:
                connection = self._connections.get(device_id)
                if connection is not None and connection.connected:
                    return connection
                raise AgentOfflineError(
                    f"Agent is offline: {device_id}"
                )
            online = [
                connection
                for connection in self._connections.values()
                if connection.connected
            ]
            if len(online) == 1:
                return online[0]
            if not online:
                raise AgentOfflineError("no authenticated Agent is online")
            raise AgentRegistryError(
                "multiple Agents are online; configure a device ID"
            )

    def snapshot(
        self,
        device_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            return self.require_online(device_id).snapshot()
        except AgentOfflineError as exc:
            return {
                "device_id": device_id,
                "connected": False,
                "status": "OFFLINE",
                "message": str(exc),
            }

    def list_connections(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                connection.snapshot()
                for _device_id, connection in sorted(
                    self._connections.items()
                )
            ]


def _message(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentProtocolError("message must be an object")
    message_type = str(value.get("type") or "").strip()
    if not message_type:
        raise AgentProtocolError("message type is required")
    return {**dict(value), "type": message_type}


def _find_forbidden(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in FORBIDDEN_COMMAND_FIELDS:
                return str(key)
            found = _find_forbidden(item)
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = _find_forbidden(item)
            if found is not None:
                return found
    return None
