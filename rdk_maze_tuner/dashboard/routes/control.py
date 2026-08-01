"""Control-lease routes for one-controller, multi-viewer operation."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from rdk_maze_tuner.platform.control_lease import (
    ControlLeaseService,
    LeasePermissionError,
    LeaseUnavailableError,
)

from .auth import AuthContext


CONTROL_LEASE_HEADER_NAME = "X-Control-Lease"


def create_control_router(
    auth: AuthContext,
    leases: ControlLeaseService,
) -> APIRouter:
    router = APIRouter(prefix="/api/control", tags=["control"])

    @router.get("/status")
    def status(request: Request) -> dict:
        principal = auth.require_principal(request)
        return leases.status_for(principal)

    @router.post("/claim")
    def claim(request: Request) -> dict:
        principal = auth.require_state_change(request)
        try:
            return leases.claim(principal).to_dict()
        except LeaseUnavailableError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "control lease is already held",
                    "control": exc.status,
                },
            ) from exc

    @router.post("/heartbeat")
    def heartbeat(request: Request) -> dict:
        principal = auth.require_state_change(request)
        try:
            return leases.heartbeat(
                principal,
                request.headers.get(CONTROL_LEASE_HEADER_NAME, ""),
            ).to_dict()
        except LeasePermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @router.post("/release")
    def release(request: Request) -> dict:
        principal = auth.require_state_change(request)
        try:
            leases.release(
                principal,
                request.headers.get(CONTROL_LEASE_HEADER_NAME, ""),
            )
        except LeasePermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return {"ok": True}

    return router
