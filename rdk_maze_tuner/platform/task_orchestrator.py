"""Thread-safe task lifecycle and stepwise maze exploration orchestration."""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Mapping, Protocol

from rdk_maze_tuner.core.device_session import DeviceDisconnectedError
from rdk_maze_tuner.core.maze_runner import MazeStepResult

from .database import Database
from .event_store import EventStore
from .modes import ModeAdapter
from .task_state import (
    InvalidTaskTransition,
    TaskStateMachine,
    TaskStatus,
)


class TaskError(RuntimeError):
    """Base task orchestration error."""


class TaskNotFoundError(TaskError):
    """Raised when a task ID is unknown."""


class TaskConflictError(TaskError):
    """Raised when an operation conflicts with the current task state."""


class TaskValidationError(TaskError):
    """Raised when a task definition is invalid."""


class TaskOperationError(TaskError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class TaskRunner(Protocol):
    def run_step(
        self,
        *,
        control: "TaskControl",
        goal: Callable[[Any, Mapping[str, Any]], bool],
        event_sink: Callable[[dict[str, Any]], None],
    ) -> MazeStepResult:
        ...


class TaskControl:
    def __init__(self) -> None:
        self._pause = threading.Event()
        self._stop = threading.Event()

    def request_pause(self) -> None:
        self._pause.set()

    def clear_pause(self) -> None:
        self._pause.clear()

    def request_stop(self) -> None:
        self._stop.set()

    def pause_requested(self) -> bool:
        return self._pause.is_set()

    def stop_requested(self) -> bool:
        return self._stop.is_set()


@dataclass
class TaskRecord:
    task_id: str
    mode: str
    map_version: str
    param_version: str
    goal: dict[str, Any]
    max_steps: int
    created_by_user_id: str | None
    created_at_utc: str
    machine: TaskStateMachine = field(default_factory=TaskStateMachine)
    control: TaskControl = field(default_factory=TaskControl)
    run_id: str | None = None
    last_run_id: str | None = None
    run_closed: bool = False
    step_count: int = 0
    preflight_result: dict[str, Any] | None = None
    adapter_snapshot: dict[str, Any] | None = None
    last_step: dict[str, Any] | None = None
    runner: TaskRunner | None = None
    worker: threading.Thread | None = None
    operation: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


class TaskOrchestrator:
    def __init__(
        self,
        *,
        database: Database,
        event_store: EventStore,
        adapters: Mapping[str, ModeAdapter],
        runner_factory: Callable[[TaskRecord], TaskRunner],
        task_id_factory: Callable[[], str] | None = None,
        run_id_factory: Callable[[], str] | None = None,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.event_store = event_store
        self.adapters = dict(adapters)
        self.runner_factory = runner_factory
        self.task_id_factory = task_id_factory or (
            lambda: f"task-{uuid.uuid4()}"
        )
        self.run_id_factory = run_id_factory or (
            lambda: f"run-{uuid.uuid4()}"
        )
        self.utc_now = utc_now or (lambda: datetime.now(UTC))
        self._condition = threading.Condition(threading.RLock())
        self._tasks: dict[str, TaskRecord] = {}
        self._closed = False

    def create_task(
        self,
        *,
        mode: str,
        map_version: str,
        param_version: str,
        goal: Mapping[str, Any],
        max_steps: int = 500,
        created_by_user_id: str | None = None,
    ) -> dict[str, Any]:
        mode = str(mode)
        if mode not in self.adapters:
            raise TaskValidationError(f"unsupported task mode: {mode}")
        map_version = _required_text(map_version, "map_version")
        param_version = _required_text(param_version, "param_version")
        normalized_goal = _normalize_goal(goal)
        if (
            not isinstance(max_steps, int)
            or isinstance(max_steps, bool)
            or not 1 <= max_steps <= 10_000
        ):
            raise TaskValidationError("max_steps must be between 1 and 10000")
        task_id = _required_text(self.task_id_factory(), "task_id")
        with self._condition:
            self._raise_if_closed_locked()
            if task_id in self._tasks:
                raise TaskConflictError(f"duplicate task_id: {task_id}")
            task = TaskRecord(
                task_id=task_id,
                mode=mode,
                map_version=map_version,
                param_version=param_version,
                goal=normalized_goal,
                max_steps=max_steps,
                created_by_user_id=created_by_user_id,
                created_at_utc=_utc_text(self.utc_now()),
            )
            self._tasks[task_id] = task
            self._emit_locked(
                task,
                "task.created",
                {
                    "mode": mode,
                    "map_version": map_version,
                    "param_version": param_version,
                    "goal": normalized_goal,
                    "max_steps": max_steps,
                },
            )
            return self._snapshot_locked(task)

    def preflight(self, task_id: str) -> dict[str, Any]:
        with self._condition:
            task = self._task_locked(task_id)
            recovering_estop = task.machine.status is TaskStatus.ESTOP
            if task.machine.status in {
                TaskStatus.COMPLETED,
                TaskStatus.LOST,
                TaskStatus.ERROR,
                TaskStatus.ESTOP,
            }:
                task.last_run_id = task.run_id or task.last_run_id
                task.run_id = None
                task.runner = None
                task.run_closed = False
            self._transition_locked(
                task,
                TaskStatus.PREFLIGHT,
                reason="manual preflight",
            )
            self._begin_operation_locked(task, "preflight")
            task.preflight_result = None
            adapter = self.adapters[task.mode]

        try:
            result = adapter.preflight()
            if result.get("ok") is not True:
                raise TaskOperationError(
                    str(result.get("code") or "PREFLIGHT_FAILED"),
                    str(result.get("message") or "preflight failed"),
                )
            if recovering_estop:
                adapter.clear_estop()
        except Exception as exc:
            self._fail_operation(task_id, "PREFLIGHT_FAILED", exc)
            self._clear_operation(task_id, "preflight")
            raise

        with self._condition:
            task = self._task_locked(task_id)
            self._clear_operation_locked(task, "preflight")
            if task.machine.status is not TaskStatus.PREFLIGHT:
                return self._snapshot_locked(task)
            task.preflight_result = dict(result)
            task.adapter_snapshot = adapter.snapshot()
            self._emit_locked(task, "task.preflight_passed", result)
            return self._snapshot_locked(task)

    def reset(self, task_id: str) -> dict[str, Any]:
        with self._condition:
            task = self._task_locked(task_id)
            if (
                task.machine.status is not TaskStatus.PREFLIGHT
                or task.preflight_result is None
            ):
                raise TaskConflictError(
                    "reset requires a successful PREFLIGHT"
                )
            self._begin_operation_locked(task, "reset")
            run_id = _required_text(self.run_id_factory(), "run_id")
            try:
                self._insert_run_locked(task, run_id)
            except Exception:
                self._clear_operation_locked(task, "reset")
                raise
            if task.run_id is not None:
                task.last_run_id = task.run_id
            task.run_id = run_id
            task.run_closed = False
            task.step_count = 0
            task.last_step = None
            task.control = TaskControl()
            adapter = self.adapters[task.mode]

        try:
            reset_result = adapter.reset(
                map_version=task.map_version,
                param_version=task.param_version,
            )
            runner = self.runner_factory(task)
        except Exception as exc:
            self._fail_operation(task_id, "RESET_FAILED", exc)
            self._clear_operation(task_id, "reset")
            raise

        with self._condition:
            task = self._task_locked(task_id)
            self._clear_operation_locked(task, "reset")
            if task.run_id != run_id:
                raise TaskConflictError("task run changed during reset")
            if task.machine.status is not TaskStatus.PREFLIGHT:
                return self._snapshot_locked(task)
            task.runner = runner
            task.adapter_snapshot = adapter.snapshot()
            self._transition_locked(task, TaskStatus.READY)
            self._emit_locked(
                task,
                "task.reset",
                {
                    "run_id": run_id,
                    "adapter": reset_result,
                },
            )
            return self._snapshot_locked(task)

    def start(self, task_id: str) -> dict[str, Any]:
        with self._condition:
            task = self._task_locked(task_id)
            if task.machine.status not in {
                TaskStatus.READY,
                TaskStatus.PAUSED,
            }:
                raise TaskConflictError(
                    f"start is not allowed from {task.machine.status.value}"
                )
            if task.runner is None or task.run_id is None:
                raise TaskConflictError("task must be reset before start")
            for other in self._tasks.values():
                if (
                    other.task_id != task.task_id
                    and other.machine.status
                    in {
                        TaskStatus.RUNNING,
                        TaskStatus.PAUSING,
                        TaskStatus.PAUSED,
                        TaskStatus.FINALIZING,
                    }
                ):
                    raise TaskConflictError(
                        f"another task is active: {other.task_id}"
                    )
            self._begin_operation_locked(task, "start")
            adapter = self.adapters[task.mode]
            resuming = task.machine.status is TaskStatus.PAUSED

        try:
            start_result = adapter.start()
        except Exception as exc:
            self._fail_operation(task_id, "START_FAILED", exc)
            self._clear_operation(task_id, "start")
            raise

        with self._condition:
            task = self._task_locked(task_id)
            self._clear_operation_locked(task, "start")
            expected = TaskStatus.PAUSED if resuming else TaskStatus.READY
            if task.machine.status is not expected:
                return self._snapshot_locked(task)
            task.control.clear_pause()
            self._transition_locked(task, TaskStatus.RUNNING)
            self._mark_run_started_locked(task)
            self._emit_locked(
                task,
                "task.resumed" if resuming else "task.started",
                {"adapter": start_result},
            )
            snapshot = self._snapshot_locked(task)
            worker = threading.Thread(
                target=self._run_loop,
                args=(task.task_id, task.run_id, task.runner, task.control),
                name=f"maze-task-{task.task_id}",
                daemon=True,
            )
            task.worker = worker
            worker.start()
            return snapshot

    def pause(self, task_id: str) -> dict[str, Any]:
        with self._condition:
            task = self._task_locked(task_id)
            if task.machine.status is not TaskStatus.RUNNING:
                raise TaskConflictError(
                    f"pause is not allowed from {task.machine.status.value}"
                )
            task.control.request_pause()
            self._transition_locked(
                task,
                TaskStatus.PAUSING,
                reason="pause requested",
            )
            self._emit_locked(task, "task.pause_requested", {})
            return self._snapshot_locked(task)

    def stop(self, task_id: str) -> dict[str, Any]:
        with self._condition:
            task = self._task_locked(task_id)
            if task.machine.status not in {
                TaskStatus.READY,
                TaskStatus.RUNNING,
                TaskStatus.PAUSING,
                TaskStatus.PAUSED,
            }:
                raise TaskConflictError(
                    f"stop is not allowed from {task.machine.status.value}"
                )
            task.control.request_stop()
            self._transition_locked(
                task,
                TaskStatus.FINALIZING,
                reason="stop requested",
            )
            self._emit_locked(task, "task.stop_requested", {})
            adapter = self.adapters[task.mode]

        try:
            result = adapter.stop()
        except Exception as exc:
            self._fail_operation(task_id, "STOP_FAILED", exc)
            raise

        with self._condition:
            task = self._task_locked(task_id)
            if task.machine.status is TaskStatus.FINALIZING:
                self._complete_locked(
                    task,
                    reason="stopped",
                    payload={"adapter": result},
                )
            return self._snapshot_locked(task)

    def estop(self, task_id: str) -> dict[str, Any]:
        with self._condition:
            task = self._task_locked(task_id)
            if task.machine.status is TaskStatus.ESTOP:
                return self._snapshot_locked(task)
            task.control.request_stop()
            try:
                self._transition_locked(
                    task,
                    TaskStatus.ESTOP,
                    reason="emergency stop",
                )
            except InvalidTaskTransition as exc:
                raise TaskConflictError(str(exc)) from exc
            self._emit_locked(task, "task.estop", {})
            adapter = self.adapters[task.mode]

        try:
            result = adapter.estop()
        except Exception as exc:
            with self._condition:
                task = self._task_locked(task_id)
                self._emit_locked(
                    task,
                    "task.estop_delivery_failed",
                    {"message": str(exc)},
                )
                self._mark_run_ended_locked(task)
            raise

        with self._condition:
            task = self._task_locked(task_id)
            self._emit_locked(task, "task.estop_delivered", result)
            self._mark_run_ended_locked(task)
            return self._snapshot_locked(task)

    def mark_lost(self, task_id: str, *, reason: str) -> dict[str, Any]:
        with self._condition:
            task = self._task_locked(task_id)
            task.control.request_stop()
            try:
                self._transition_locked(
                    task,
                    TaskStatus.LOST,
                    reason=reason,
                )
            except InvalidTaskTransition as exc:
                raise TaskConflictError(str(exc)) from exc
            self._emit_locked(
                task,
                "task.lost",
                {"reason": reason},
            )
            self._mark_run_ended_locked(task)
            return self._snapshot_locked(task)

    def switch_mode(self, task_id: str, *, mode: str) -> dict[str, Any]:
        with self._condition:
            task = self._task_locked(task_id)
            if not task.machine.can_switch_mode:
                raise TaskConflictError(
                    f"mode switch is not allowed from {task.machine.status.value}"
                )
            if mode not in self.adapters:
                raise TaskValidationError(f"unsupported task mode: {mode}")
            task.mode = mode
            self._emit_locked(task, "task.mode_changed", {"mode": mode})
            return self._snapshot_locked(task)

    def snapshot(self, task_id: str) -> dict[str, Any]:
        with self._condition:
            return self._snapshot_locked(self._task_locked(task_id))

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._condition:
            return [
                self._snapshot_locked(task)
                for task in self._tasks.values()
            ]

    def command_owner(self) -> dict[str, Any] | None:
        """Return the task that currently owns motion command authority."""
        command_states = {
            TaskStatus.RUNNING,
            TaskStatus.PAUSING,
            TaskStatus.PAUSED,
            TaskStatus.FINALIZING,
        }
        with self._condition:
            owners = [
                task
                for task in self._tasks.values()
                if task.machine.status in command_states
            ]
            if len(owners) > 1:
                raise TaskConflictError(
                    "multiple tasks hold command ownership"
                )
            if not owners:
                return None
            return self._snapshot_locked(owners[0])

    def wait_for_state(
        self,
        task_id: str,
        statuses: set[TaskStatus],
        *,
        timeout_s: float,
    ) -> dict[str, Any]:
        expected = {TaskStatus(status) for status in statuses}
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while True:
                task = self._task_locked(task_id)
                if task.machine.status in expected:
                    return self._snapshot_locked(task)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"timeout waiting for task {task_id} state"
                    )
                self._condition.wait(remaining)

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            workers = []
            active_adapters: set[ModeAdapter] = set()
            for task in self._tasks.values():
                task.control.request_stop()
                if task.worker is not None and task.worker.is_alive():
                    workers.append(task.worker)
                if task.machine.status in {
                    TaskStatus.PREFLIGHT,
                    TaskStatus.READY,
                    TaskStatus.RUNNING,
                    TaskStatus.PAUSING,
                    TaskStatus.PAUSED,
                    TaskStatus.FINALIZING,
                }:
                    try:
                        self._transition_locked(
                            task,
                            TaskStatus.LOST,
                            reason="orchestrator shutdown",
                        )
                    except TaskConflictError:
                        pass
                    self._emit_locked(
                        task,
                        "task.lost",
                        {"reason": "orchestrator shutdown"},
                    )
                    self._mark_run_ended_locked(task)
                    active_adapters.add(self.adapters[task.mode])
        for adapter in active_adapters:
            try:
                adapter.stop()
            except Exception:
                pass
        for worker in workers:
            worker.join(timeout=1.0)
        for adapter in set(self.adapters.values()):
            adapter.close()

    def _run_loop(
        self,
        task_id: str,
        run_id: str,
        runner: TaskRunner,
        control: TaskControl,
    ) -> None:
        goal = self._goal_for(task_id)
        while True:
            with self._condition:
                task = self._task_locked(task_id)
                if task.run_id != run_id or task.machine.status not in {
                    TaskStatus.RUNNING,
                    TaskStatus.PAUSING,
                }:
                    return
            try:
                result = runner.run_step(
                    control=control,
                    goal=goal,
                    event_sink=lambda event: self._record_runner_event(
                        task_id,
                        run_id,
                        event,
                    ),
                )
            except DeviceDisconnectedError as exc:
                with self._condition:
                    task = self._task_locked(task_id)
                    if task.run_id != run_id or task.machine.status in {
                        TaskStatus.COMPLETED,
                        TaskStatus.ESTOP,
                        TaskStatus.LOST,
                        TaskStatus.FINALIZING,
                    }:
                        return
                    self._transition_locked(
                        task,
                        TaskStatus.LOST,
                        reason=str(exc),
                    )
                    self._emit_locked(
                        task,
                        "task.lost",
                        {"reason": str(exc)},
                    )
                    self._mark_run_ended_locked(task)
                return
            except Exception as exc:
                with self._condition:
                    task = self._task_locked(task_id)
                    if task.run_id != run_id or task.machine.status in {
                        TaskStatus.COMPLETED,
                        TaskStatus.ESTOP,
                        TaskStatus.LOST,
                        TaskStatus.FINALIZING,
                    }:
                        return
                    self._transition_locked(
                        task,
                        TaskStatus.ERROR,
                        reason=str(exc),
                    )
                    self._emit_locked(
                        task,
                        "task.error",
                        {
                            "code": "STEP_FAILED",
                            "message": str(exc),
                        },
                    )
                    self._mark_run_ended_locked(task)
                return

            with self._condition:
                task = self._task_locked(task_id)
                if (
                    task.run_id != run_id
                    or task.machine.status
                    not in {
                        TaskStatus.RUNNING,
                        TaskStatus.PAUSING,
                    }
                ):
                    return
                task.last_step = _step_snapshot(result)
                if result.outcome not in {"paused", "stopped"}:
                    task.step_count += 1
                status = task.machine.status
                pause_requested = control.pause_requested()

            if status in {
                TaskStatus.COMPLETED,
                TaskStatus.ESTOP,
                TaskStatus.LOST,
                TaskStatus.ERROR,
                TaskStatus.FINALIZING,
            }:
                return
            if result.outcome == "paused" or pause_requested:
                self._pause_at_boundary(task_id, run_id)
                return
            if result.outcome == "stopped" or control.stop_requested():
                return
            if result.outcome == "goal_reached":
                self._finish_success(task_id, run_id, reason="goal_reached")
                return
            if result.outcome == "exhausted":
                with self._condition:
                    task = self._task_locked(task_id)
                    exploration_complete = (
                        task.goal["type"] == "exploration_complete"
                    )
                if exploration_complete:
                    self._finish_success(
                        task_id,
                        run_id,
                        reason="exploration_complete",
                    )
                else:
                    self._finish_error(
                        task_id,
                        run_id,
                        code="NO_PATH",
                        message="planner exhausted before reaching goal",
                    )
                return

            with self._condition:
                task = self._task_locked(task_id)
                if task.step_count >= task.max_steps:
                    pass
                else:
                    continue
            self._finish_error(
                task_id,
                run_id,
                code="MAX_STEPS",
                message="task exceeded configured max_steps",
            )
            return

    def _pause_at_boundary(self, task_id: str, run_id: str) -> None:
        with self._condition:
            task = self._task_locked(task_id)
            if (
                task.run_id != run_id
                or task.machine.status is not TaskStatus.PAUSING
            ):
                return
            adapter = self.adapters[task.mode]
        try:
            result = adapter.pause()
        except Exception as exc:
            self._finish_error(
                task_id,
                run_id,
                code="PAUSE_FAILED",
                message=str(exc),
            )
            return
        with self._condition:
            task = self._task_locked(task_id)
            if (
                task.run_id == run_id
                and task.machine.status is TaskStatus.PAUSING
            ):
                self._transition_locked(task, TaskStatus.PAUSED)
                self._emit_locked(
                    task,
                    "task.paused",
                    {"adapter": result},
                )

    def _finish_success(
        self,
        task_id: str,
        run_id: str,
        *,
        reason: str,
    ) -> None:
        with self._condition:
            task = self._task_locked(task_id)
            if (
                task.run_id != run_id
                or task.machine.status is not TaskStatus.RUNNING
            ):
                return
            self._transition_locked(
                task,
                TaskStatus.FINALIZING,
                reason=reason,
            )
            adapter = self.adapters[task.mode]
        try:
            result = adapter.stop()
        except Exception as exc:
            self._finish_error(
                task_id,
                run_id,
                code="FINALIZE_FAILED",
                message=str(exc),
            )
            return
        with self._condition:
            task = self._task_locked(task_id)
            if task.machine.status is TaskStatus.FINALIZING:
                self._complete_locked(
                    task,
                    reason=reason,
                    payload={"adapter": result},
                )

    def _finish_error(
        self,
        task_id: str,
        run_id: str,
        *,
        code: str,
        message: str,
    ) -> None:
        with self._condition:
            task = self._task_locked(task_id)
            if task.run_id != run_id or task.machine.status in {
                TaskStatus.COMPLETED,
                TaskStatus.ESTOP,
                TaskStatus.LOST,
                TaskStatus.ERROR,
            }:
                return
            try:
                self._transition_locked(
                    task,
                    TaskStatus.ERROR,
                    reason=message,
                )
            except InvalidTaskTransition:
                return
            self._emit_locked(
                task,
                "task.error",
                {"code": code, "message": message},
            )
            self._mark_run_ended_locked(task)

    def _complete_locked(
        self,
        task: TaskRecord,
        *,
        reason: str,
        payload: Mapping[str, Any],
    ) -> None:
        self._transition_locked(
            task,
            TaskStatus.COMPLETED,
            reason=reason,
        )
        self._emit_locked(
            task,
            "task.completed",
            {"reason": reason, **dict(payload)},
        )
        self._mark_run_ended_locked(task)

    def _fail_operation(
        self,
        task_id: str,
        code: str,
        error: Exception,
    ) -> None:
        with self._condition:
            task = self._task_locked(task_id)
            if task.machine.status not in {
                TaskStatus.ERROR,
                TaskStatus.ESTOP,
                TaskStatus.LOST,
            }:
                try:
                    self._transition_locked(
                        task,
                        TaskStatus.ERROR,
                        reason=str(error),
                    )
                except InvalidTaskTransition:
                    return
            self._emit_locked(
                task,
                "task.error",
                {"code": code, "message": str(error)},
            )
            self._mark_run_ended_locked(task)

    def _record_runner_event(
        self,
        task_id: str,
        run_id: str,
        event: Mapping[str, Any],
    ) -> None:
        with self._condition:
            task = self._task_locked(task_id)
            if task.run_id != run_id:
                return
            self._emit_locked(
                task,
                str(event.get("type") or "runner.event"),
                event.get("payload"),
                source="maze_runner",
            )

    def _goal_for(
        self,
        task_id: str,
    ) -> Callable[[Any, Mapping[str, Any]], bool]:
        with self._condition:
            goal = dict(self._task_locked(task_id).goal)
        if goal["type"] == "cell":
            cell = tuple(goal["cell"])
            return lambda maze, _telemetry: maze.position == cell
        return lambda _maze, _telemetry: False

    def _transition_locked(
        self,
        task: TaskRecord,
        target: TaskStatus,
        *,
        reason: str | None = None,
    ) -> None:
        previous = task.machine.status
        try:
            task.machine.transition(target, reason=reason)
        except InvalidTaskTransition as exc:
            raise TaskConflictError(str(exc)) from exc
        if task.run_id is not None and not task.run_closed:
            with self.database.connection() as connection:
                connection.execute(
                    "UPDATE runs SET status = ? WHERE id = ?",
                    (target.value, task.run_id),
                )
        self._emit_locked(
            task,
            "task.state_changed",
            {
                "from": previous.value,
                "to": target.value,
                "reason": reason,
            },
        )
        self._condition.notify_all()

    def _insert_run_locked(self, task: TaskRecord, run_id: str) -> None:
        metadata = json.dumps(
            {
                "task_id": task.task_id,
                "map_version": task.map_version,
                "param_version": task.param_version,
                "goal": task.goal,
                "max_steps": task.max_steps,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    id, mode, status, created_by_user_id,
                    created_at_utc, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    task.mode,
                    TaskStatus.PREFLIGHT.value,
                    task.created_by_user_id,
                    _utc_text(self.utc_now()),
                    metadata,
                ),
            )

    def _mark_run_started_locked(self, task: TaskRecord) -> None:
        if task.run_id is None:
            return
        with self.database.connection() as connection:
            connection.execute(
                """
                UPDATE runs
                SET started_at_utc = COALESCE(started_at_utc, ?)
                WHERE id = ?
                """,
                (_utc_text(self.utc_now()), task.run_id),
            )

    def _mark_run_ended_locked(self, task: TaskRecord) -> None:
        if task.run_id is None or task.run_closed:
            return
        with self.database.connection() as connection:
            connection.execute(
                """
                UPDATE runs
                SET status = ?, ended_at_utc = COALESCE(ended_at_utc, ?)
                WHERE id = ?
                """,
                (
                    task.machine.status.value,
                    _utc_text(self.utc_now()),
                    task.run_id,
                ),
            )
        task.run_closed = True

    def _emit_locked(
        self,
        task: TaskRecord,
        event_type: str,
        payload: Any,
        *,
        source: str = "task_orchestrator",
    ) -> None:
        row = {
            "type": event_type,
            "source": source,
            "payload": _json_ready(payload),
            "utc_timestamp": _utc_text(self.utc_now()),
        }
        task.events.append(row)
        if task.run_id is not None and not task.run_closed:
            self.event_store.append(
                run_id=task.run_id,
                event_type=event_type,
                source=source,
                payload=row["payload"],
            )

    def _snapshot_locked(self, task: TaskRecord) -> dict[str, Any]:
        state = task.machine.snapshot()
        return {
            "task_id": task.task_id,
            "run_id": task.run_id,
            "last_run_id": task.last_run_id,
            "mode": task.mode,
            "map_version": task.map_version,
            "param_version": task.param_version,
            "goal": _json_ready(task.goal),
            "max_steps": task.max_steps,
            "step_count": task.step_count,
            "created_by_user_id": task.created_by_user_id,
            "created_at_utc": task.created_at_utc,
            "preflight": _json_ready(task.preflight_result),
            "adapter": _json_ready(task.adapter_snapshot),
            "last_step": _json_ready(task.last_step),
            "event_count": len(task.events),
            "recent_events": _json_ready(task.events[-40:]),
            "operation": task.operation,
            **state,
        }

    def _task_locked(self, task_id: str) -> TaskRecord:
        task = self._tasks.get(str(task_id))
        if task is None:
            raise TaskNotFoundError(f"unknown task: {task_id}")
        return task

    def _begin_operation_locked(
        self,
        task: TaskRecord,
        operation: str,
    ) -> None:
        if task.operation is not None:
            raise TaskConflictError(
                f"task operation already in progress: {task.operation}"
            )
        task.operation = operation

    def _clear_operation(
        self,
        task_id: str,
        operation: str,
    ) -> None:
        with self._condition:
            task = self._task_locked(task_id)
            self._clear_operation_locked(task, operation)

    @staticmethod
    def _clear_operation_locked(
        task: TaskRecord,
        operation: str,
    ) -> None:
        if task.operation == operation:
            task.operation = None

    def _raise_if_closed_locked(self) -> None:
        if self._closed:
            raise TaskConflictError("task orchestrator is closed")


def _normalize_goal(goal: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(goal, Mapping):
        raise TaskValidationError("goal must be an object")
    goal_type = str(goal.get("type") or "")
    if goal_type == "cell":
        cell = goal.get("cell")
        if (
            not isinstance(cell, (list, tuple))
            or len(cell) != 2
            or any(
                not isinstance(value, int) or isinstance(value, bool)
                for value in cell
            )
        ):
            raise TaskValidationError(
                "cell goal requires two integer coordinates"
            )
        return {"type": "cell", "cell": [int(cell[0]), int(cell[1])]}
    if goal_type == "exploration_complete":
        return {"type": "exploration_complete"}
    raise TaskValidationError(
        "goal.type must be cell or exploration_complete"
    )


def _required_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise TaskValidationError(f"{name} is required")
    return text


def _step_snapshot(result: MazeStepResult) -> dict[str, Any]:
    return {
        "action": result.action.name,
        "action_id": result.action_id,
        "outcome": result.outcome,
        "telemetry": _json_ready(result.telemetry),
        "done": _json_ready(result.done),
    }


def _json_ready(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return _json_ready(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    return value


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC clock must return a timezone-aware datetime")
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
