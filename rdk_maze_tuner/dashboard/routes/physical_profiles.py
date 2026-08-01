"""Authenticated, read-only physical-profile API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from rdk_maze_tuner.platform.physical_profile_repository import (
    PhysicalProfileNotFoundError,
    PhysicalProfileRepository,
)

from .auth import AuthContext


def create_physical_profiles_router(
    auth: AuthContext,
    repository: PhysicalProfileRepository,
) -> APIRouter:
    router = APIRouter(tags=["physical-profiles"])

    @router.get("/api/physical-profiles")
    def list_profiles(request: Request) -> dict[str, Any]:
        auth.require_principal(request)
        return {
            "physical_profiles": [
                profile.to_dict()
                for profile in repository.list_profiles()
            ]
        }

    @router.get("/api/physical-profiles/{profile_id}")
    def get_profile(
        profile_id: str,
        request: Request,
    ) -> dict[str, Any]:
        auth.require_principal(request)
        try:
            return repository.get(profile_id).to_dict()
        except PhysicalProfileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router
