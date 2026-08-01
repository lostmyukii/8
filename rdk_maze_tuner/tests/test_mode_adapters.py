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
            "fw": "maze-webots-sim",
            "version": "0.1.0",
        }

    def request_ack(self, message_type, **fields):
        self.calls.append((message_type, fields))
        return {"type": "ack", "seq": len(self.calls), "ok": True}

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
