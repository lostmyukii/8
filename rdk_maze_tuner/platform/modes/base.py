"""Common contract for simulation and real-car operating modes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ModeAdapterError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class ModeAdapter(ABC):
    mode: str

    @abstractmethod
    def preflight(
        self,
        *,
        map_version: str | None = None,
        param_version: str | None = None,
        physical_profile_id: str | None = None,
    ) -> dict[str, Any]:
        ...

    @abstractmethod
    def reset(
        self,
        *,
        map_version: str,
        param_version: str,
        physical_profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...

    @abstractmethod
    def start(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def pause(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def stop(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def estop(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def clear_estop(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def close(self) -> None:
        ...
