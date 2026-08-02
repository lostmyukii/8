"""Lease-guarded single-step coordinate debugging."""

from __future__ import annotations

import itertools
from dataclasses import asdict, is_dataclass
from enum import Enum
from threading import Lock
from typing import Any, Mapping

from fastapi import APIRouter, HTTPException, Request

from rdk_maze_tuner.core.goal_directed_planner import (
    GoalDirectedPlanner,
    GoalPlanningError,
)
from rdk_maze_tuner.core.maze_map import Direction, MazeMap, PlannedAction
from rdk_maze_tuner.core.maze_runner import MazeRunner
from rdk_maze_tuner.core.serial_client import SerialClientError
from rdk_maze_tuner.core.task_pose_tracker import TaskPoseTracker
from rdk_maze_tuner.dashboard.state import DashboardState
from rdk_maze_tuner.platform.control_lease import (
    ControlLeaseService,
    LeasePermissionError,
)
from rdk_maze_tuner.platform.map_repository import (
    MapNotFoundError,
    MapRepository,
)
from rdk_maze_tuner.platform.task_orchestrator import (
    TaskConflictError,
    TaskOrchestrator,
)

from .auth import AuthContext
from .control import CONTROL_LEASE_HEADER_NAME


class _SingleActionPlanner:
    def __init__(self, action: PlannedAction) -> None:
        self.action = action

    def next_action(self, _maze: MazeMap) -> PlannedAction:
        return self.action


def create_debug_router(
    auth: AuthContext,
    leases: ControlLeaseService,
    orchestrator: TaskOrchestrator,
    maps: MapRepository,
    state: DashboardState,
) -> APIRouter:
    router = APIRouter(prefix="/api/debug", tags=["debug"])
    execution_lock = Lock()
    request_ids = itertools.count(1)

    @router.post("/step")
    async def debug_step(request: Request) -> dict[str, Any]:
        principal = auth.require_state_change(request)
        _require_control(request, leases, principal)
        owner = _command_owner(orchestrator)
        if owner is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "single-step debug is blocked while active task "
                    f"{owner['task_id']} owns motion commands"
                ),
            )
        body = await _json_body(request)
        execute = body.get("execute", False)
        if not isinstance(execute, bool):
            raise _validation_error(
                "DEBUG_EXECUTE_INVALID",
                "execute must be a boolean",
            )
        map_version = body.get("map_version")
        if not isinstance(map_version, str) or not map_version.strip():
            raise _validation_error(
                "DEBUG_MAP_REQUIRED",
                "map_version is required",
            )
        target = _target_cell(body.get("target_cell"))
        try:
            version = maps.get_version(map_version.strip())
        except MapNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        current = state.snapshot()["maze"]
        working_maze = MazeMap.from_definition(
            version.definition,
            wall_threshold_mm=int(
                state.params.get("tof.wall_threshold_mm")
            ),
            map_version_id=version.version_id,
        )
        _anchor_working_maze(working_maze, current)
        _require_in_bounds(working_maze, target)
        planner = GoalDirectedPlanner(goal_cells=(target,))
        try:
            route = planner.plan_route(working_maze)
        except GoalPlanningError as exc:
            raise _validation_error(
                "DEBUG_TARGET_UNREACHABLE",
                exc.message,
            ) from exc
        action = route.actions[0] if route.actions else None
        preview = {
            "mode": "execute" if execute else "preview",
            "executed": False,
            "map_version": version.version_id,
            "map_digest": version.digest,
            "current_cell": list(working_maze.position),
            "target_cell": [target[0], target[1]],
            "next_action": _action_payload(action),
            "route": route.to_dict(),
        }
        if not execute:
            state.record("debug.preview", preview)
            leases.audit_operation(
                principal,
                "debug_step_preview",
                details={
                    "map_version": version.version_id,
                    "target_cell": [target[0], target[1]],
                    "action": (
                        None if action is None else action.name
                    ),
                },
            )
            return preview
        if action is None:
            raise _validation_error(
                "DEBUG_ALREADY_AT_TARGET",
                "current reliable cell already equals the debug target",
            )
        if state.client is None:
            raise HTTPException(
                status_code=503,
                detail="single-step debug has no connected device session",
            )
        if not execution_lock.acquire(blocking=False):
            raise HTTPException(
                status_code=409,
                detail="another single-step debug action is still running",
            )
        try:
            if _command_owner(orchestrator) is not None:
                raise HTTPException(
                    status_code=409,
                    detail="an automatic task acquired motion commands",
                )
            state.set_maze(working_maze)
            request_id = next(request_ids)
            pose_tracker = TaskPoseTracker.from_params(
                maze=working_maze,
                params=state.params,
                arrival_config=(
                    state.params.arrival_verification_config()
                ),
                run_id=f"debug-{request_id:06d}",
            )

            def emit_debug(event: Mapping[str, Any]) -> None:
                state.record(
                    f"debug.{event.get('type') or 'event'}",
                    event.get("payload"),
                )

            runner = MazeRunner(
                client=state.client,
                params=state.params,
                maze=working_maze,
                planner=_SingleActionPlanner(action),
                action_prefix=f"debug-{request_id:06d}",
                pose_tracker=pose_tracker,
                enable_recovery=False,
            )
            result = runner.run_step(event_sink=emit_debug)
            if (
                result.outcome == "continue"
                and result.reliable_pose is not None
            ):
                state.commit_debug_pose()
        except SerialClientError as exc:
            state.record(
                "debug.error",
                {"code": "DEVICE_ERROR", "message": str(exc)},
            )
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        finally:
            execution_lock.release()

        response = {
            **preview,
            "executed": True,
            "result": _result_payload(result),
        }
        state.record(
            "debug.executed",
            {
                "action_id": result.action_id,
                "action": result.action.name,
                "outcome": result.outcome,
                "error_code": result.error_code,
            },
        )
        leases.audit_operation(
            principal,
            "debug_step_execute",
            details={
                "map_version": version.version_id,
                "target_cell": [target[0], target[1]],
                "action_id": result.action_id,
                "action": result.action.name,
                "outcome": result.outcome,
            },
        )
        return response

    return router


def _command_owner(
    orchestrator: TaskOrchestrator,
) -> dict[str, Any] | None:
    try:
        return orchestrator.command_owner()
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _require_control(request, leases, principal) -> None:
    try:
        leases.require_holder(
            principal,
            request.headers.get(CONTROL_LEASE_HEADER_NAME),
        )
    except LeasePermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = {}
    return body if isinstance(body, dict) else {}


def _target_cell(value: object) -> tuple[int, int]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or not all(
            isinstance(item, int) and not isinstance(item, bool)
            for item in value
        )
    ):
        raise _validation_error(
            "DEBUG_TARGET_INVALID",
            "target_cell must be an integer [x, y] pair",
        )
    return int(value[0]), int(value[1])


def _require_in_bounds(maze: MazeMap, target: tuple[int, int]) -> None:
    if (
        maze.cols is None
        or maze.rows is None
        or not (0 <= target[0] < maze.cols)
        or not (0 <= target[1] < maze.rows)
    ):
        raise _validation_error(
            "DEBUG_TARGET_OUT_OF_BOUNDS",
            "target_cell is outside the selected map",
        )


def _anchor_working_maze(
    maze: MazeMap,
    current: Mapping[str, Any],
) -> None:
    position = current.get("position")
    heading = current.get("heading")
    if (
        isinstance(position, (list, tuple))
        and len(position) == 2
        and all(
            isinstance(item, int) and not isinstance(item, bool)
            for item in position
        )
        and maze.cols is not None
        and maze.rows is not None
        and 0 <= position[0] < maze.cols
        and 0 <= position[1] < maze.rows
    ):
        maze.position = (int(position[0]), int(position[1]))
        maze.visited.add(maze.position)
        maze.cell(maze.position).visited = True
    try:
        maze.heading = Direction(str(heading))
    except ValueError:
        pass


def _action_payload(
    action: PlannedAction | None,
) -> dict[str, Any] | None:
    if action is None:
        return None
    return {
        "name": action.name,
        "direction": (
            None if action.direction is None else action.direction.value
        ),
    }


def _result_payload(result) -> dict[str, Any]:
    return {
        "action": _action_payload(result.action),
        "action_id": result.action_id,
        "outcome": result.outcome,
        "done": _json_ready(result.done),
        "motion_target": _json_ready(result.motion_target),
        "evidence": _json_ready(result.evidence),
        "reliable_pose": _json_ready(result.reliable_pose),
        "error_code": result.error_code,
    }


def _json_ready(value: Any) -> Any:
    if is_dataclass(value):
        return _json_ready(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _json_ready(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(item) for item in value]
    return value


def _validation_error(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"code": code, "message": message},
    )
