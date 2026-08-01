"""Webots simulation mode adapter."""

from __future__ import annotations

from typing import Any, Callable

from rdk_maze_tuner.core.device_session import DeviceSession
from rdk_maze_tuner.core.serial_client import SerialClient
from rdk_maze_tuner.core.tcp_stream import open_tcp

from .base import ModeAdapter


DEFAULT_SIMULATION_ENDPOINT = "127.0.0.1:8765"


def _open_simulation_session(endpoint: str) -> DeviceSession:
    stream = open_tcp(endpoint)
    return DeviceSession(SerialClient(stream, timeout_s=3.0))


class SimulationModeAdapter(ModeAdapter):
    mode = "simulation"

    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_SIMULATION_ENDPOINT,
        session_factory: Callable[[str], DeviceSession] = _open_simulation_session,
        ready_timeout_s: float = 3.0,
    ) -> None:
        self.endpoint = endpoint
        self._session_factory = session_factory
        self._ready_timeout_s = ready_timeout_s
        self._session: DeviceSession | None = None

    def preflight(self) -> dict[str, Any]:
        session = self._get_session()
        session.start()
        ready = session.wait_ready(timeout_s=self._ready_timeout_s)
        return {
            "ok": True,
            "mode": self.mode,
            "code": "READY",
            "ready": ready,
        }

    def reset(
        self,
        *,
        map_version: str,
        param_version: str,
    ) -> dict[str, Any]:
        ack = self._get_session().request_ack(
            "reset",
            map_version=map_version,
            param_version=param_version,
        )
        return self._result("reset", ack)

    def start(self) -> dict[str, Any]:
        return self._result("start", self._get_session().request_ack("start"))

    def pause(self) -> dict[str, Any]:
        return self._result("pause", self._get_session().request_ack("pause"))

    def stop(self) -> dict[str, Any]:
        return self._result("stop", self._get_session().stop())

    def estop(self) -> dict[str, Any]:
        return self._result(
            "estop",
            self._get_session().estop(reason="dashboard"),
        )

    def clear_estop(self) -> dict[str, Any]:
        return self._result(
            "clear_estop",
            self._get_session().request_ack("clear_estop"),
        )

    def snapshot(self) -> dict[str, Any]:
        session_snapshot = (
            {
                "connected": False,
                "ready": None,
                "telemetry": None,
                "last_error": None,
            }
            if self._session is None
            else self._session.snapshot()
        )
        return {
            "mode": self.mode,
            "endpoint": self.endpoint,
            "status": (
                "ONLINE" if session_snapshot["connected"] else "OFFLINE"
            ),
            **session_snapshot,
        }

    def close(self) -> None:
        if self._session is not None:
            self._session.close()

    @property
    def session(self) -> DeviceSession:
        if self._session is None:
            raise RuntimeError("simulation preflight has not opened a session")
        return self._session

    def _get_session(self) -> DeviceSession:
        if self._session is None:
            self._session = self._session_factory(self.endpoint)
        return self._session

    def _result(self, command: str, ack: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": self.mode,
            "command": command,
            "ack": ack,
        }
