import json
import socket
import time
from typing import Any, Mapping, get_type_hints

from simulation.webots.maze_car.engine_contract import (
    SimulationProtocolEngine,
)
from simulation.webots.maze_car.controllers.maze_sim_controller.sim_engine import (
    MazeSimEngine,
)
from simulation.webots.maze_car.controllers.maze_sim_controller.sim_server import (
    SimProtocolServer,
)


class FakeEngine:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []
        self.tick_count = 0

    def ready_message(self) -> dict[str, Any]:
        self.events.append(("ready", None))
        return {"type": "ready", "fw": "fake-engine"}

    def telemetry_message(self) -> dict[str, Any]:
        self.events.append(("telemetry", None))
        return {"type": "telemetry", "state": "IDLE"}

    def handle(
        self,
        message: Mapping[str, Any],
        *,
        now_ms: int,
    ) -> list[dict[str, Any]]:
        self.events.append(("handle", (dict(message), now_ms)))
        return [
            {
                "type": "ack",
                "seq": int(message.get("seq") or 0),
                "ok": True,
            }
        ]

    def tick(self, *, now_ms: int) -> list[dict[str, Any]]:
        self.tick_count += 1
        self.events.append(("tick", now_ms))
        return []

    def on_client_connected(self, *, now_ms: int) -> None:
        self.events.append(("connected", now_ms))

    def on_client_disconnected(self, *, now_ms: int) -> None:
        self.events.append(("disconnected", now_ms))

    def close(self) -> None:
        self.events.append(("close", None))


def _read_lines(
    client: socket.socket,
    *,
    expected_count: int,
) -> list[dict[str, Any]]:
    client.settimeout(0.1)
    deadline = time.monotonic() + 1.0
    payload = bytearray()
    while payload.count(b"\n") < expected_count and time.monotonic() < deadline:
        try:
            chunk = client.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            break
        payload.extend(chunk)
    return [
        json.loads(line)
        for line in bytes(payload).decode("utf-8").splitlines()
        if line
    ]


def test_server_depends_on_protocol_instead_of_concrete_engine():
    annotation = get_type_hints(SimProtocolServer.__init__)["engine"]

    assert annotation is SimulationProtocolEngine
    assert isinstance(FakeEngine(), SimulationProtocolEngine)
    assert isinstance(MazeSimEngine(), SimulationProtocolEngine)


def test_server_notifies_connection_before_initial_frames_and_disconnect_once():
    engine = FakeEngine()
    server = SimProtocolServer(engine, port=0)
    client = socket.create_connection(server.listener.getsockname())
    try:
        for now_ms in range(100, 200):
            server.poll(now_ms=now_ms)
            if any(event[0] == "connected" for event in engine.events):
                break
            time.sleep(0.001)

        assert _read_lines(client, expected_count=2) == [
            {"type": "ready", "fw": "fake-engine"},
            {"type": "telemetry", "state": "IDLE"},
        ]
        initial_frames = [
            event for event in engine.events if event[0] != "tick"
        ][:3]
        assert initial_frames == [
            ("connected", now_ms),
            ("ready", None),
            ("telemetry", None),
        ]

        client.close()
        for now_ms in range(125, 225):
            server.poll(now_ms=now_ms)
            if any(event[0] == "disconnected" for event in engine.events):
                break
            time.sleep(0.001)

        disconnected = [
            event
            for event in engine.events
            if event[0] == "disconnected"
        ]
        assert len(disconnected) == 1
        assert disconnected[0][1] >= 125
    finally:
        client.close()
        server.close()

    assert engine.events[-1] == ("close", None)


def test_server_routes_newline_json_and_keeps_invalid_json_contract():
    engine = FakeEngine()
    server = SimProtocolServer(engine, port=0)
    client = socket.create_connection(server.listener.getsockname())
    try:
        for now_ms in range(0, 80):
            server.poll(now_ms=now_ms)
            if any(event[0] == "connected" for event in engine.events):
                break
            time.sleep(0.001)
        _read_lines(client, expected_count=2)
        client.sendall(
            b'{"type":"heartbeat","seq":7}\n'
            b'not-json\n'
        )

        for now_ms in range(80, 180):
            server.poll(now_ms=now_ms)
            if any(event[0] == "handle" for event in engine.events):
                break
            time.sleep(0.001)
        replies = _read_lines(client, expected_count=2)

        assert replies[0] == {
            "type": "ack",
            "seq": 7,
            "ok": True,
        }
        assert replies[1]["type"] == "error"
        assert replies[1]["code"] == "INVALID_JSON"
        handled = [
            event
            for event in engine.events
            if event[0] == "handle"
        ]
        assert len(handled) == 1
        assert handled[0][1][0] == {"type": "heartbeat", "seq": 7}
        assert handled[0][1][1] >= 80
    finally:
        client.close()
        server.close()


def test_close_is_idempotent_and_closes_engine_once():
    engine = FakeEngine()
    server = SimProtocolServer(engine, port=0)

    server.close()
    server.close()

    assert engine.events == [("close", None)]
