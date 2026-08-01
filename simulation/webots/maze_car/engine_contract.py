"""Transport-facing contract shared by deterministic and physical engines."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable


@runtime_checkable
class SimulationProtocolEngine(Protocol):
    """Minimal behavior required by the localhost simulation server."""

    def ready_message(self) -> dict[str, Any]:
        ...

    def telemetry_message(self) -> dict[str, Any]:
        ...

    def handle(
        self,
        message: Mapping[str, Any],
        *,
        now_ms: int,
    ) -> list[dict[str, Any]]:
        ...

    def tick(self, *, now_ms: int) -> list[dict[str, Any]]:
        ...

    def on_client_connected(self, *, now_ms: int) -> None:
        ...

    def on_client_disconnected(self, *, now_ms: int) -> None:
        ...

    def close(self) -> None:
        ...
