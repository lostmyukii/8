"""Validated lifecycle states for one maze task."""

from __future__ import annotations

from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    IDLE = "IDLE"
    PREFLIGHT = "PREFLIGHT"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    FINALIZING = "FINALIZING"
    COMPLETED = "COMPLETED"
    LOST = "LOST"
    ERROR = "ERROR"
    ESTOP = "ESTOP"


class InvalidTaskTransition(RuntimeError):
    def __init__(self, current: TaskStatus, target: TaskStatus) -> None:
        super().__init__(
            f"invalid task transition: {current.value} -> {target.value}"
        )
        self.current = current
        self.target = target


_RECOVERY_STATES = {
    TaskStatus.LOST,
    TaskStatus.ERROR,
    TaskStatus.ESTOP,
}

_ALLOWED: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.IDLE: frozenset(
        {
            TaskStatus.PREFLIGHT,
            TaskStatus.ESTOP,
        }
    ),
    TaskStatus.PREFLIGHT: frozenset(
        {
            TaskStatus.READY,
            TaskStatus.LOST,
            TaskStatus.ERROR,
            TaskStatus.ESTOP,
        }
    ),
    TaskStatus.READY: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.PREFLIGHT,
            TaskStatus.FINALIZING,
            TaskStatus.LOST,
            TaskStatus.ERROR,
            TaskStatus.ESTOP,
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.PAUSING,
            TaskStatus.FINALIZING,
            TaskStatus.LOST,
            TaskStatus.ERROR,
            TaskStatus.ESTOP,
        }
    ),
    TaskStatus.PAUSING: frozenset(
        {
            TaskStatus.PAUSED,
            TaskStatus.FINALIZING,
            TaskStatus.LOST,
            TaskStatus.ERROR,
            TaskStatus.ESTOP,
        }
    ),
    TaskStatus.PAUSED: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.FINALIZING,
            TaskStatus.PREFLIGHT,
            TaskStatus.LOST,
            TaskStatus.ERROR,
            TaskStatus.ESTOP,
        }
    ),
    TaskStatus.FINALIZING: frozenset(
        {
            TaskStatus.COMPLETED,
            TaskStatus.LOST,
            TaskStatus.ERROR,
            TaskStatus.ESTOP,
        }
    ),
    TaskStatus.COMPLETED: frozenset(
        {
            TaskStatus.PREFLIGHT,
            TaskStatus.ESTOP,
        }
    ),
    TaskStatus.LOST: frozenset(
        {
            TaskStatus.PREFLIGHT,
            TaskStatus.ESTOP,
        }
    ),
    TaskStatus.ERROR: frozenset(
        {
            TaskStatus.PREFLIGHT,
            TaskStatus.ESTOP,
        }
    ),
    TaskStatus.ESTOP: frozenset({TaskStatus.PREFLIGHT}),
}


class TaskStateMachine:
    def __init__(self, status: TaskStatus = TaskStatus.IDLE) -> None:
        self._status = TaskStatus(status)
        self._revision = 0
        self._reason: str | None = None
        self._previous: TaskStatus | None = None

    @property
    def status(self) -> TaskStatus:
        return self._status

    @property
    def can_switch_mode(self) -> bool:
        return self._status in {TaskStatus.IDLE, TaskStatus.COMPLETED}

    @property
    def requires_manual_recovery(self) -> bool:
        return self._status in _RECOVERY_STATES

    def transition(
        self,
        target: TaskStatus,
        *,
        reason: str | None = None,
    ) -> TaskStatus:
        target = TaskStatus(target)
        if target not in _ALLOWED[self._status]:
            raise InvalidTaskTransition(self._status, target)
        self._previous = self._status
        self._status = target
        self._reason = None if reason is None else str(reason)
        self._revision += 1
        return self._status

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self._status.value,
            "previous_status": (
                None if self._previous is None else self._previous.value
            ),
            "reason": self._reason,
            "revision": self._revision,
            "can_switch_mode": self.can_switch_mode,
            "requires_manual_recovery": self.requires_manual_recovery,
        }
