"""Authenticated, lease-guarded task orchestration routes."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request

from rdk_maze_tuner.core.serial_client import SerialClientError
from rdk_maze_tuner.platform.control_lease import (
    ControlLeaseService,
    LeasePermissionError,
)
from rdk_maze_tuner.platform.modes import ModeAdapterError
from rdk_maze_tuner.platform.task_orchestrator import (
    TaskConflictError,
    TaskNotFoundError,
    TaskOperationError,
    TaskOrchestrator,
    TaskValidationError,
)

from .auth import AuthContext
from .control import CONTROL_LEASE_HEADER_NAME


def create_tasks_router(
    auth: AuthContext,
    leases: ControlLeaseService,
    orchestrator: TaskOrchestrator,
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/tasks", status_code=201)
    async def create_task(request: Request) -> dict[str, Any]:
        principal = auth.require_state_change(request)
        _require_control(request, leases, principal)
        body = await _json_body(request)
        result = _task_call(
            lambda: orchestrator.create_task(
                mode=body.get("mode"),
                map_version=body.get("map_version"),
                param_version=body.get("param_version"),
                goal=body.get("goal"),
                max_steps=body.get("max_steps", 500),
                created_by_user_id=principal.user_id,
            )
        )
        leases.audit_operation(
            principal,
            "task_create",
            details={
                "task_id": result["task_id"],
                "mode": result["mode"],
            },
        )
        return result

    @router.post("/api/tasks/{task_id}/preflight")
    def preflight(task_id: str, request: Request) -> dict[str, Any]:
        return _holder_operation(
            request,
            task_id,
            "preflight",
            auth,
            leases,
            lambda: orchestrator.preflight(task_id),
        )

    @router.post("/api/tasks/{task_id}/reset")
    def reset(task_id: str, request: Request) -> dict[str, Any]:
        return _holder_operation(
            request,
            task_id,
            "reset",
            auth,
            leases,
            lambda: orchestrator.reset(task_id),
        )

    @router.post("/api/tasks/{task_id}/start")
    def start(task_id: str, request: Request) -> dict[str, Any]:
        return _holder_operation(
            request,
            task_id,
            "start",
            auth,
            leases,
            lambda: orchestrator.start(task_id),
        )

    @router.post("/api/tasks/{task_id}/pause")
    def pause(task_id: str, request: Request) -> dict[str, Any]:
        return _holder_operation(
            request,
            task_id,
            "pause",
            auth,
            leases,
            lambda: orchestrator.pause(task_id),
        )

    @router.post("/api/tasks/{task_id}/stop")
    def stop(task_id: str, request: Request) -> dict[str, Any]:
        return _holder_operation(
            request,
            task_id,
            "stop",
            auth,
            leases,
            lambda: orchestrator.stop(task_id),
        )

    @router.post("/api/tasks/{task_id}/estop")
    def estop(task_id: str, request: Request) -> dict[str, Any]:
        principal = auth.require_state_change(request)
        result = _task_call(lambda: orchestrator.estop(task_id))
        leases.audit_operation(
            principal,
            "task_estop",
            details={"task_id": task_id},
        )
        return result

    return router


def _holder_operation(
    request: Request,
    task_id: str,
    operation: str,
    auth: AuthContext,
    leases: ControlLeaseService,
    callback: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    principal = auth.require_state_change(request)
    _require_control(request, leases, principal)
    result = _task_call(callback)
    leases.audit_operation(
        principal,
        f"task_{operation}",
        details={"task_id": task_id, "run_id": result.get("run_id")},
    )
    return result


def _require_control(request, leases, principal) -> None:
    try:
        leases.require_holder(
            principal,
            request.headers.get(CONTROL_LEASE_HEADER_NAME),
        )
    except LeasePermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _task_call(callback: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return callback()
    except TaskValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TaskOperationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (ModeAdapterError, SerialClientError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}
