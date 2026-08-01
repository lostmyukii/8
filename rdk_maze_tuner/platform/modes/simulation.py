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
        map_provider: Callable[[str], Any] | None = None,
        ready_timeout_s: float = 3.0,
    ) -> None:
        self.endpoint = endpoint
        self._session_factory = session_factory
        self._map_provider = map_provider
        self._ready_timeout_s = ready_timeout_s
        self._session: DeviceSession | None = None
        self._loaded_map: dict[str, Any] | None = None

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
        if self._map_provider is not None:
            version = self._map_provider(map_version)
            if version is None:
                raise ModeAdapterError(
                    "MAP_NOT_FOUND",
                    f"map version does not exist: {map_version}",
                )
            payload = (
                version.to_dict()
                if hasattr(version, "to_dict")
                else dict(version)
            )
            version_id = str(
                payload.get("version_id") or map_version
            )
            digest = str(payload.get("digest") or "")
            definition = payload.get("definition")
            load_ack = self._get_session().request_ack(
                "load_map",
                map_version_id=version_id,
                digest=digest,
                definition=definition,
            )
            self._validate_map_ack(
                load_ack,
                map_version_id=version_id,
                digest=digest,
            )
            self._loaded_map = {
                "map_version_id": version_id,
                "digest": digest,
            }
            ack = self._get_session().request_ack(
                "reset",
                map_version_id=version_id,
                digest=digest,
                param_version=param_version,
            )
            self._validate_map_ack(
                ack,
                map_version_id=version_id,
                digest=digest,
            )
            return self._result("reset", ack)
        ack = self._get_session().request_ack(
            "reset",
            map_version=map_version,
            param_version=param_version,
        )
        return self._result("reset", ack)

    def start(self) -> dict[str, Any]:
        fields = self._loaded_map or {}
        ack = self._get_session().request_ack("start", **fields)
        if self._loaded_map is not None:
            self._validate_map_ack(ack, **self._loaded_map)
        return self._result("start", ack)

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
            "loaded_map": (
                None
                if self._loaded_map is None
                else dict(self._loaded_map)
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

    @staticmethod
    def _validate_map_ack(
        ack: dict[str, Any],
        *,
        map_version_id: str,
        digest: str,
    ) -> None:
        if (
            ack.get("map_version_id") != map_version_id
            or ack.get("digest") != digest
        ):
            raise ModeAdapterError(
                "MAP_ACK_MISMATCH",
                "simulation acknowledged a different map version or digest",
            )
