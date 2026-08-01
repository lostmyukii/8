"""Authenticated routes for immutable grid-maze versions and source images."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from rdk_maze_tuner.core.maze_validation import MazeValidationError
from rdk_maze_tuner.platform.control_lease import (
    ControlLeaseService,
    LeasePermissionError,
)
from rdk_maze_tuner.platform.map_repository import (
    MapConflictError,
    MapNotFoundError,
    MapRepository,
)

from .auth import AuthContext
from .control import CONTROL_LEASE_HEADER_NAME


def create_maps_router(
    auth: AuthContext,
    leases: ControlLeaseService,
    repository: MapRepository,
) -> APIRouter:
    router = APIRouter(tags=["maps"])

    @router.get("/api/maps")
    def list_maps(request: Request) -> dict[str, Any]:
        auth.require_principal(request)
        return {"maps": repository.list_maps()}

    @router.get("/api/maps/{map_id}/versions")
    def list_versions(map_id: str, request: Request) -> dict[str, Any]:
        auth.require_principal(request)
        try:
            return {"versions": repository.list_versions(map_id)}
        except MapNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/api/map-versions/{version_id}")
    def get_version(version_id: str, request: Request) -> dict[str, Any]:
        auth.require_principal(request)
        try:
            return repository.get_version(version_id).to_dict()
        except MapNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/api/maps", status_code=201)
    async def create_map(request: Request) -> dict[str, Any]:
        principal = auth.require_state_change(request)
        _require_control(request, leases, principal)
        body = await _json_body(request)
        try:
            map_record, version = repository.create_map(
                name=body.get("name"),
                definition=body.get("definition"),
                created_by_user_id=principal.user_id,
            )
        except (MazeValidationError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        leases.audit_operation(
            principal,
            "map_create",
            details={
                "map_id": map_record["map_id"],
                "version_id": version.version_id,
                "digest": version.digest,
            },
        )
        return {"map": map_record, "version": version.to_dict()}

    @router.post("/api/maps/{map_id}/versions", status_code=201)
    async def save_version(
        map_id: str,
        request: Request,
    ) -> dict[str, Any]:
        principal = auth.require_state_change(request)
        _require_control(request, leases, principal)
        body = await _json_body(request)
        try:
            version = repository.save_version(
                map_id=map_id,
                definition=body.get("definition"),
                created_by_user_id=principal.user_id,
            )
        except (MazeValidationError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except MapNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except MapConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        leases.audit_operation(
            principal,
            "map_version_create",
            details={
                "map_id": map_id,
                "version_id": version.version_id,
                "version_number": version.version_number,
                "digest": version.digest,
            },
        )
        return {"version": version.to_dict()}

    @router.post(
        "/api/maps/{map_id}/source-image",
        status_code=201,
    )
    async def store_source_image(
        map_id: str,
        request: Request,
        filename: str = "source-image",
    ) -> dict[str, Any]:
        principal = auth.require_state_change(request)
        _require_control(request, leases, principal)
        try:
            artifact = repository.store_source_image(
                map_id=map_id,
                filename=filename,
                content_type=(
                    request.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                ),
                content=await request.body(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from exc
        except MapNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except MapConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        leases.audit_operation(
            principal,
            "map_source_image_store",
            details={
                "map_id": map_id,
                "artifact_id": artifact["artifact_id"],
                "sha256": artifact["sha256"],
            },
        )
        return {"artifact": artifact}

    return router


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
        return {}
    return body if isinstance(body, dict) else {}
