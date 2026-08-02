"""Webots simulation mode adapter."""

from __future__ import annotations

import os
from typing import Any, Callable

from rdk_maze_tuner.core.device_session import DeviceSession
from rdk_maze_tuner.core.maze_validation import validate_map_definition
from rdk_maze_tuner.core.serial_client import SerialClient
from rdk_maze_tuner.core.tcp_stream import open_tcp
from simulation.webots.maze_car.physical_preflight import (
    PhysicalGeometryPreflight,
)

from .base import ModeAdapter, ModeAdapterError


DEFAULT_SIMULATION_ENDPOINT = "127.0.0.1:8765"
DEFAULT_ACTION_RESULT_TIMEOUT_S = 15.0


def _open_simulation_session(endpoint: str) -> DeviceSession:
    stream = open_tcp(endpoint)
    return DeviceSession(
        SerialClient(stream, timeout_s=3.0),
        action_result_timeout_s=DEFAULT_ACTION_RESULT_TIMEOUT_S,
    )


class SimulationModeAdapter(ModeAdapter):
    mode = "simulation"

    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_SIMULATION_ENDPOINT,
        session_factory: Callable[[str], DeviceSession] = _open_simulation_session,
        map_provider: Callable[[str], Any] | None = None,
        physical_profile_provider: Callable[[str], Any] | None = None,
        ready_timeout_s: float = 3.0,
    ) -> None:
        self.endpoint = endpoint
        self._session_factory = session_factory
        self._map_provider = map_provider
        self._physical_profile_provider = physical_profile_provider
        self._ready_timeout_s = ready_timeout_s
        self._session: DeviceSession | None = None
        self._loaded_map: dict[str, Any] | None = None
        self._loaded_profile: dict[str, Any] | None = None

    def preflight(
        self,
        *,
        map_version: str | None = None,
        param_version: str | None = None,
        physical_profile_id: str | None = None,
    ) -> dict[str, Any]:
        session = self._get_session()
        session.start()
        ready = session.wait_ready(timeout_s=self._ready_timeout_s)
        result = {
            "ok": True,
            "mode": self.mode,
            "code": "READY",
            "ready": ready,
            "controller_version": str(
                ready.get("version") or "unknown"
            ),
            "webots_version": str(
                ready.get("webots_version")
                or os.environ.get("MAZE_WEBOTS_VERSION")
                or "unknown"
            ),
        }
        if physical_profile_id is not None:
            supports_physical = (
                ready.get("simulation_backend") == "physical"
                or ready.get("fw") == "maze-webots-physical"
                or "physical_profiles" in ready.get("features", ())
            )
            if not supports_physical:
                raise ModeAdapterError(
                    "PHYSICAL_BACKEND_REQUIRED",
                    "this task requires the Webots physical backend",
                )
            if self._physical_profile_provider is None:
                result["physical_profile"] = {
                    "profile_id": physical_profile_id
                }
            else:
                profile = self._physical_profile_provider(
                    physical_profile_id
                )
                if profile is None:
                    raise ModeAdapterError(
                        "PHYSICAL_PROFILE_NOT_FOUND",
                        "physical profile does not exist: "
                        f"{physical_profile_id}",
                    )
                result["physical_profile"] = _profile_payload(profile)
            physical_profile = result["physical_profile"]
            if (
                map_version is not None
                and self._map_provider is not None
                and "digest" in physical_profile
            ):
                version = self._map_provider(map_version)
                if version is None:
                    raise ModeAdapterError(
                        "MAP_NOT_FOUND",
                        f"map version does not exist: {map_version}",
                    )
                map_payload = (
                    version.to_dict()
                    if hasattr(version, "to_dict")
                    else dict(version)
                )
                snapshot = physical_profile.get("snapshot")
                geometry = (
                    snapshot.get("geometry")
                    if isinstance(snapshot, dict)
                    else None
                )
                if not isinstance(geometry, dict):
                    raise ModeAdapterError(
                        "PHYSICAL_PROFILE_INVALID",
                        "physical profile snapshot geometry is missing",
                    )
                definition = validate_map_definition(
                    map_payload.get("definition")
                )
                report = PhysicalGeometryPreflight(
                    chassis_length_mm=float(
                        geometry["chassis_length_m"]
                    )
                    * 1_000.0,
                    chassis_width_mm=float(
                        geometry["chassis_width_m"]
                    )
                    * 1_000.0,
                ).check(
                    definition,
                    map_version_id=str(
                        map_payload.get("version_id") or map_version
                    ),
                )
                result["physical_preflight"] = report.to_dict()
                if not report.ok:
                    result.update(
                        ok=False,
                        code=report.code,
                        message=(
                            "物理通道不安全：净通道 "
                            f"{report.actual_passage_x_mm:g} × "
                            f"{report.actual_passage_y_mm:g} mm，"
                            "至少需要 "
                            f"{report.minimum_required_passage_mm:g} mm"
                        ),
                    )
        return result

    def reset(
        self,
        *,
        map_version: str,
        param_version: str,
        physical_profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if physical_profile is not None:
            profile = _profile_payload(physical_profile)
            profile_id = str(profile.get("profile_id") or "")
            digest = str(profile.get("digest") or "")
            load_profile_ack = self._get_session().request_ack(
                "load_profile",
                physical_profile_id=profile_id,
                digest=digest,
            )
            self._validate_profile_ack(
                load_profile_ack,
                physical_profile_id=profile_id,
                digest=digest,
                reset_identity=False,
            )
            self._loaded_profile = {
                "physical_profile_id": profile_id,
                "physical_profile_digest": digest,
            }
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
                map_digest=digest,
                param_version=param_version,
                **(self._loaded_profile or {}),
            )
            self._validate_map_ack(
                ack,
                map_version_id=version_id,
                digest=digest,
                reset_identity=True,
            )
            if self._loaded_profile is not None:
                self._validate_profile_ack(
                    ack,
                    physical_profile_id=self._loaded_profile[
                        "physical_profile_id"
                    ],
                    digest=self._loaded_profile[
                        "physical_profile_digest"
                    ],
                    reset_identity=True,
                )
            return self._result("reset", ack)
        ack = self._get_session().request_ack(
            "reset",
            map_version=map_version,
            param_version=param_version,
            **(self._loaded_profile or {}),
        )
        if self._loaded_profile is not None:
            self._validate_profile_ack(
                ack,
                physical_profile_id=self._loaded_profile[
                    "physical_profile_id"
                ],
                digest=self._loaded_profile[
                    "physical_profile_digest"
                ],
                reset_identity=True,
            )
        return self._result("reset", ack)

    def start(self) -> dict[str, Any]:
        fields = dict(self._loaded_profile or {})
        if self._loaded_map is not None:
            fields.update(
                map_version_id=self._loaded_map["map_version_id"],
                map_digest=self._loaded_map["digest"],
            )
        ack = self._get_session().request_ack("start", **fields)
        if self._loaded_map is not None:
            self._validate_map_ack(
                ack,
                map_version_id=self._loaded_map["map_version_id"],
                digest=self._loaded_map["digest"],
                reset_identity=True,
            )
        if self._loaded_profile is not None:
            self._validate_profile_ack(
                ack,
                physical_profile_id=self._loaded_profile[
                    "physical_profile_id"
                ],
                digest=self._loaded_profile[
                    "physical_profile_digest"
                ],
                reset_identity=True,
            )
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
            "loaded_profile": (
                None
                if self._loaded_profile is None
                else dict(self._loaded_profile)
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
        reset_identity: bool = False,
    ) -> None:
        acknowledged_digest = ack.get(
            "map_digest" if reset_identity else "digest"
        )
        if acknowledged_digest is None:
            acknowledged_digest = ack.get("digest")
        if (
            ack.get("map_version_id") != map_version_id
            or acknowledged_digest != digest
        ):
            raise ModeAdapterError(
                "MAP_ACK_MISMATCH",
                "simulation acknowledged a different map version or digest",
            )

    @staticmethod
    def _validate_profile_ack(
        ack: dict[str, Any],
        *,
        physical_profile_id: str,
        digest: str,
        reset_identity: bool,
    ) -> None:
        acknowledged_digest = ack.get(
            "physical_profile_digest" if reset_identity else "digest"
        )
        if (
            ack.get("physical_profile_id") != physical_profile_id
            or acknowledged_digest != digest
        ):
            raise ModeAdapterError(
                "PHYSICAL_PROFILE_ACK_MISMATCH",
                "simulation acknowledged a different physical profile "
                "or digest",
            )


def _profile_payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if not isinstance(value, dict):
        try:
            value = dict(value)
        except (TypeError, ValueError) as exc:
            raise ModeAdapterError(
                "PHYSICAL_PROFILE_INVALID",
                "physical profile must be an object",
            ) from exc
    profile_id = str(value.get("profile_id") or "")
    digest = str(value.get("digest") or "")
    if not profile_id or len(digest) != 64:
        raise ModeAdapterError(
            "PHYSICAL_PROFILE_INVALID",
            "physical profile ID and 64-character digest are required",
        )
    return dict(value)
