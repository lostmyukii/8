"""Device administration and authenticated outbound Agent WebSocket."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket
from starlette.websockets import WebSocketDisconnect

from rdk_maze_tuner.platform.agent_registry import (
    AgentProtocolError,
    AgentRegistry,
)
from rdk_maze_tuner.platform.control_lease import (
    ControlLeaseService,
    LeasePermissionError,
)
from rdk_maze_tuner.platform.device_tokens import (
    DeviceAuthenticationError,
    DeviceTokenError,
    DeviceTokenService,
)

from .auth import AuthContext
from .control import CONTROL_LEASE_HEADER_NAME


def create_agents_router(
    auth: AuthContext,
    leases: ControlLeaseService,
    tokens: DeviceTokenService,
    registry: AgentRegistry,
) -> APIRouter:
    router = APIRouter(tags=["agents"])

    @router.get("/api/agents")
    def list_agents(request: Request) -> dict[str, Any]:
        auth.require_principal(request)
        online = {
            item["device_id"]: item
            for item in registry.list_connections()
        }
        devices = []
        for device in tokens.list_devices():
            connection = online.get(device["device_id"])
            devices.append(
                {
                    **device,
                    "connected": bool(
                        connection and connection["connected"]
                    ),
                }
            )
        return {"devices": devices}

    @router.post("/api/agents/devices", status_code=201)
    async def register_device(request: Request) -> dict[str, Any]:
        principal = auth.require_state_change(request)
        _require_control(request, leases, principal)
        body = await _json_body(request)
        try:
            result = tokens.register(
                device_id=body.get("device_id"),
                name=body.get("name"),
                metadata=body.get("metadata"),
            )
        except (ValueError, DeviceTokenError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        leases.audit_operation(
            principal,
            "agent_device_register",
            details={"device_id": result["device_id"]},
        )
        return result

    @router.post("/api/agents/devices/{device_id}/rotate")
    def rotate_device(
        device_id: str,
        request: Request,
    ) -> dict[str, Any]:
        principal = auth.require_state_change(request)
        _require_control(request, leases, principal)
        try:
            result = tokens.rotate(device_id)
        except (ValueError, DeviceTokenError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        leases.audit_operation(
            principal,
            "agent_device_rotate",
            details={"device_id": device_id},
        )
        return result

    @router.post("/api/agents/devices/{device_id}/revoke")
    def revoke_device(
        device_id: str,
        request: Request,
    ) -> dict[str, Any]:
        principal = auth.require_state_change(request)
        _require_control(request, leases, principal)
        try:
            result = tokens.revoke(device_id)
        except (ValueError, DeviceTokenError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        leases.audit_operation(
            principal,
            "agent_device_revoke",
            details={"device_id": device_id},
        )
        return result

    @router.websocket("/ws/agents/{device_id}")
    async def agent_socket(
        websocket: WebSocket,
        device_id: str,
    ) -> None:
        token = _bearer_token(
            websocket.headers.get("authorization")
        )
        try:
            principal = tokens.authenticate(device_id, token)
        except (ValueError, DeviceAuthenticationError):
            await websocket.close(code=4401)
            return
        connection = registry.connect(principal)
        tokens.set_status(device_id, "online")
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "agent.welcome",
                "schema_version": 1,
                "payload": {"device_id": device_id},
            }
        )
        try:
            while True:
                command = connection.next_outbound()
                if command is not None:
                    await websocket.send_json(command)
                try:
                    message = await asyncio.wait_for(
                        websocket.receive_json(),
                        timeout=0.1,
                    )
                except TimeoutError:
                    continue
                try:
                    connection.receive(message)
                except AgentProtocolError:
                    await websocket.close(code=4400)
                    return
        except WebSocketDisconnect:
            return
        finally:
            registry.disconnect(device_id, connection)
            tokens.set_status(device_id, "lost")

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


def _bearer_token(header: str | None) -> str:
    if not isinstance(header, str):
        return ""
    scheme, _, token = header.partition(" ")
    if scheme.casefold() != "bearer":
        return ""
    return token.strip()
