"""Offline-safe placeholder for the future RDK X3 Agent mode."""

from __future__ import annotations

from typing import Any, NoReturn

from .base import ModeAdapter, ModeAdapterError


DEVICE_OFFLINE = "DEVICE_OFFLINE"
DEVICE_OFFLINE_MESSAGE = "RDK X3 Agent is offline"


class RealModeAdapter(ModeAdapter):
    mode = "real"

    def preflight(
        self,
        *,
        map_version: str | None = None,
        param_version: str | None = None,
        physical_profile_id: str | None = None,
    ) -> dict[str, Any]:
        if physical_profile_id:
            raise ModeAdapterError(
                "PHYSICAL_PROFILE_NOT_APPLICABLE",
                "Webots physical profiles cannot be used in real mode",
            )
        return {
            "ok": False,
            "mode": self.mode,
            "code": DEVICE_OFFLINE,
            "message": DEVICE_OFFLINE_MESSAGE,
        }

    def reset(
        self,
        *,
        map_version: str,
        param_version: str,
        physical_profile: dict[str, Any] | None = None,
    ) -> NoReturn:
        if physical_profile is not None:
            raise ModeAdapterError(
                "PHYSICAL_PROFILE_NOT_APPLICABLE",
                "Webots physical profiles cannot be used in real mode",
            )
        self._offline()

    def start(self) -> NoReturn:
        self._offline()

    def pause(self) -> NoReturn:
        self._offline()

    def stop(self) -> NoReturn:
        self._offline()

    def estop(self) -> NoReturn:
        self._offline()

    def clear_estop(self) -> NoReturn:
        self._offline()

    def snapshot(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "status": DEVICE_OFFLINE,
            "connected": False,
            "message": DEVICE_OFFLINE_MESSAGE,
        }

    def close(self) -> None:
        return None

    @staticmethod
    def _offline() -> NoReturn:
        raise ModeAdapterError(DEVICE_OFFLINE, DEVICE_OFFLINE_MESSAGE)
