import pytest

from rdk_maze_tuner.platform.modes import (
    ModeAdapterError,
    RealModeAdapter,
    SimulationModeAdapter,
)


class FakeSession:
    def __init__(self):
        self.started = False
        self.closed = False
        self.calls = []
        self.last_telemetry = {"type": "telemetry", "state": "IDLE"}

    @property
    def connected(self):
        return self.started and not self.closed

    def start(self):
        self.started = True

    def wait_ready(self, *, timeout_s=None):
        return {
            "type": "ready",
            "fw": "maze-webots-physical",
            "version": "0.2.0",
            "webots_version": "R2025a",
        }

    def request_ack(self, message_type, **fields):
        self.calls.append((message_type, fields))
        result = {"type": "ack", "seq": len(self.calls), "ok": True}
        if message_type == "load_profile":
            result.update(
                physical_profile_id=fields["physical_profile_id"],
                digest=fields["digest"],
            )
        elif message_type == "load_map":
            result.update(
                map_version_id=fields["map_version_id"],
                digest=fields["digest"],
            )
        elif message_type in {"reset", "start"}:
            result.update(
                map_version_id=fields.get("map_version_id"),
                map_digest=fields.get("map_digest"),
                physical_profile_id=fields.get("physical_profile_id"),
                physical_profile_digest=fields.get(
                    "physical_profile_digest"
                ),
            )
        return result

    def stop(self):
        self.calls.append(("stop", {}))
        return {"type": "ack", "seq": len(self.calls), "ok": True}

    def estop(self, *, reason="rdk"):
        self.calls.append(("estop", {"reason": reason}))
        return {"type": "ack", "seq": len(self.calls), "ok": True}

    def snapshot(self):
        return {
            "connected": self.connected,
            "ready": {"type": "ready", "fw": "maze-webots-sim"},
            "telemetry": self.last_telemetry,
            "last_error": None,
        }

    def close(self):
        self.closed = True


def test_simulation_adapter_uses_default_endpoint_and_unified_commands():
    session = FakeSession()
    endpoints = []

    def session_factory(endpoint):
        endpoints.append(endpoint)
        return session

    adapter = SimulationModeAdapter(session_factory=session_factory)

    assert adapter.preflight()["ok"] is True
    assert endpoints == ["127.0.0.1:8765"]
    assert adapter.reset(map_version="map-v1", param_version="param-v2")["ok"] is True
    assert adapter.start()["ok"] is True
    assert adapter.pause()["ok"] is True
    assert adapter.stop()["ok"] is True
    assert adapter.estop()["ok"] is True
    assert adapter.clear_estop()["ok"] is True
    assert adapter.snapshot()["mode"] == "simulation"
    assert session.calls == [
        ("reset", {"map_version": "map-v1", "param_version": "param-v2"}),
        ("start", {}),
        ("pause", {}),
        ("stop", {}),
        ("estop", {"reason": "dashboard"}),
        ("clear_estop", {}),
    ]

    adapter.close()
    assert session.closed is True


def test_real_adapter_reports_explicit_device_offline_until_agent_exists():
    adapter = RealModeAdapter()

    preflight = adapter.preflight()
    assert preflight == {
        "ok": False,
        "mode": "real",
        "code": "DEVICE_OFFLINE",
        "message": "RDK X3 Agent is offline",
    }
    assert adapter.snapshot()["status"] == "DEVICE_OFFLINE"

    for operation in (
        lambda: adapter.reset(map_version="map-v1", param_version="param-v1"),
        adapter.start,
        adapter.pause,
        adapter.stop,
        adapter.estop,
        adapter.clear_estop,
    ):
        with pytest.raises(ModeAdapterError) as exc:
            operation()
        assert exc.value.code == "DEVICE_OFFLINE"


def test_simulation_loads_profile_then_map_and_validates_identity_acks():
    session = FakeSession()
    profile = {
        "profile_id": "normal-v1",
        "digest": "a" * 64,
        "random_seed": 20260801,
        "snapshot": {
            "profile_id": "normal-v1",
            "geometry": {
                "chassis_length_m": 0.23,
                "chassis_width_m": 0.16,
            },
        },
    }
    map_version = {
        "version_id": "mapv-1",
        "digest": "b" * 64,
        "definition": {
            "rows": 2,
            "cols": 2,
            "cell_width_mm": 450,
            "cell_height_mm": 450,
            "wall_thickness_mm": 40,
            "wall_height_mm": 180,
            "start": {"x": 0, "y": 1, "heading": "N"},
            "goals": [{"x": 1, "y": 0}],
            "walls": [
                {"x1": 0, "y1": 0, "x2": 2, "y2": 0},
                {"x1": 2, "y1": 0, "x2": 2, "y2": 2},
                {"x1": 2, "y1": 2, "x2": 0, "y2": 2},
                {"x1": 0, "y1": 2, "x2": 0, "y2": 0},
            ],
            "source_image_digest": None,
        },
    }
    adapter = SimulationModeAdapter(
        session_factory=lambda _endpoint: session,
        map_provider=lambda _version: map_version,
        physical_profile_provider=lambda _profile: profile,
    )

    preflight = adapter.preflight(
        map_version="mapv-1",
        param_version="param-v1",
        physical_profile_id="normal-v1",
    )
    reset = adapter.reset(
        map_version="mapv-1",
        param_version="param-v1",
        physical_profile=profile,
    )
    started = adapter.start()

    assert preflight["physical_profile"]["digest"] == "a" * 64
    assert reset["ok"] is True
    assert started["ok"] is True
    assert [name for name, _fields in session.calls[:4]] == [
        "load_profile",
        "load_map",
        "reset",
        "start",
    ]
    assert adapter.snapshot()["loaded_profile"] == {
        "physical_profile_id": "normal-v1",
        "physical_profile_digest": "a" * 64,
    }


def test_real_mode_rejects_webots_profile_as_not_applicable():
    adapter = RealModeAdapter()

    with pytest.raises(ModeAdapterError) as exc:
        adapter.preflight(
            map_version="map-v1",
            param_version="param-v1",
            physical_profile_id="normal-v1",
        )

    assert exc.value.code == "PHYSICAL_PROFILE_NOT_APPLICABLE"


def test_simulation_preflight_rejects_unsafe_map_from_task_context():
    session = FakeSession()
    unsafe_definition = {
        "rows": 2,
        "cols": 2,
        "cell_width_mm": 300,
        "cell_height_mm": 300,
        "wall_thickness_mm": 20,
        "wall_height_mm": 180,
        "start": {"x": 0, "y": 1, "heading": "N"},
        "goals": [{"x": 1, "y": 0}],
        "walls": [
            {"x1": 0, "y1": 0, "x2": 2, "y2": 0},
            {"x1": 2, "y1": 0, "x2": 2, "y2": 2},
            {"x1": 2, "y1": 2, "x2": 0, "y2": 2},
            {"x1": 0, "y1": 2, "x2": 0, "y2": 0},
        ],
        "source_image_digest": None,
    }
    profile = {
        "profile_id": "normal-v1",
        "digest": "a" * 64,
        "random_seed": 20260801,
        "snapshot": {
            "geometry": {
                "chassis_length_m": 0.23,
                "chassis_width_m": 0.16,
            }
        },
    }
    adapter = SimulationModeAdapter(
        session_factory=lambda _endpoint: session,
        map_provider=lambda _version: {
            "version_id": "unsafe-v1",
            "digest": "b" * 64,
            "definition": unsafe_definition,
        },
        physical_profile_provider=lambda _profile: profile,
    )

    result = adapter.preflight(
        map_version="unsafe-v1",
        param_version="param-v1",
        physical_profile_id="normal-v1",
    )

    assert result["ok"] is False
    assert result["code"] == "MAP_GEOMETRY_UNSAFE"
    assert result["physical_preflight"]["actual_passage_x_mm"] == 280.0
