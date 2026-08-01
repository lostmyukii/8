"""Authenticated run evidence, score, and synchronized replay routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from rdk_maze_tuner.platform.replay import (
    ReplayService,
    RunNotFoundError,
)

from .auth import AuthContext


def create_runs_router(
    auth: AuthContext,
    replay: ReplayService,
) -> APIRouter:
    router = APIRouter(tags=["runs"])

    @router.get("/api/runs")
    def list_runs(
        request: Request,
        limit: int = 50,
    ) -> dict[str, Any]:
        auth.require_principal(request)
        try:
            return {"runs": replay.list_runs(limit=limit)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/runs/{run_id}")
    def get_run(run_id: str, request: Request) -> dict[str, Any]:
        auth.require_principal(request)
        try:
            return replay.get_run(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/runs/{run_id}/events")
    def get_events(run_id: str, request: Request) -> dict[str, Any]:
        auth.require_principal(request)
        try:
            return {
                "run_id": run_id,
                "events": replay.list_events(run_id),
            }
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/runs/{run_id}/replay")
    def get_replay(run_id: str, request: Request) -> dict[str, Any]:
        auth.require_principal(request)
        try:
            return replay.get_manifest(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/runs/{run_id}/video", response_class=FileResponse)
    def get_video(run_id: str, request: Request) -> FileResponse:
        auth.require_principal(request)
        try:
            path = replay.video_path(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if path is None:
            raise HTTPException(
                status_code=404,
                detail="complete video artifact is not available",
            )
        return FileResponse(
            path,
            media_type="video/mp4",
            headers={"Cache-Control": "private, no-store"},
        )

    return router
