from copy import deepcopy

from rdk_maze_tuner.core.maze_validation import validate_map_definition
from rdk_maze_tuner.platform.modes import SimulationModeAdapter
from simulation.webots.maze_car.controllers.maze_sim_controller.sim_engine import (
    MazeSimEngine,
)
from simulation.webots.maze_car.map_loader import compile_map


def definition_payload() -> dict:
    return {
        "rows": 2,
        "cols": 3,
        "cell_width_mm": 400,
        "cell_height_mm": 300,
        "wall_thickness_mm": 20,
        "wall_height_mm": 140,
        "start": {"x": 0, "y": 1, "heading": "E"},
        "goals": [{"x": 2, "y": 0}],
        "walls": [
            {"x1": 0, "y1": 0, "x2": 3, "y2": 0},
            {"x1": 3, "y1": 0, "x2": 3, "y2": 2},
            {"x1": 3, "y1": 2, "x2": 0, "y2": 2},
            {"x1": 0, "y1": 2, "x2": 0, "y2": 0},
            {"x1": 1, "y1": 0, "x2": 1, "y2": 1},
        ],
        "source_image_digest": None,
    }


def load_message(*, seq=21, version_id="map-v3", payload=None):
    definition = validate_map_definition(payload or definition_payload())
    return {
        "type": "load_map",
        "seq": seq,
        "map_version_id": version_id,
        "digest": definition.content_digest,
        "definition": definition.to_dict(),
    }


def test_compile_map_generates_engine_edges_and_webots_wall_nodes():
    definition = validate_map_definition(definition_payload())

    compiled = compile_map(definition)

    assert compiled.rows == 2
    assert compiled.cols == 3
    assert compiled.cell_width_m == 0.4
    assert compiled.cell_height_m == 0.3
    assert compiled.start_cell == (0, 1)
    assert compiled.start_heading == "E"
    assert frozenset(((0, 0), (1, 0))) in compiled.internal_walls
    assert len(compiled.wall_nodes) == len(definition.walls)
    assert all("boundingObject Box" in node for node in compiled.wall_nodes)


def test_sim_engine_load_map_and_reset_ack_echo_version_and_digest():
    engine = MazeSimEngine()
    load = load_message()

    loaded = engine.handle(load, now_ms=0)
    reset = engine.handle(
        {"type": "reset", "seq": 22, "map_version_id": "map-v3"},
        now_ms=1,
    )

    assert loaded == [
        {
            "type": "ack",
            "seq": 21,
            "ok": True,
            "map_version_id": "map-v3",
            "digest": load["digest"],
        }
    ]
    assert reset[0]["seq"] == 22
    assert reset[0]["map_version_id"] == "map-v3"
    assert reset[0]["digest"] == load["digest"]
    assert engine.cell == (0, 1)
    assert engine.heading == "E"
    telemetry = reset[1]
    assert telemetry["map_version_id"] == "map-v3"
    assert telemetry["map_digest"] == load["digest"]


def test_sim_engine_rejects_digest_mismatch_and_loading_during_action():
    engine = MazeSimEngine()
    message = load_message()
    mismatch = deepcopy(message)
    mismatch["seq"] = 30
    mismatch["digest"] = "0" * 64

    rejected = engine.handle(mismatch, now_ms=0)
    assert rejected[0]["ok"] is False
    assert "digest" in rejected[0]["message"]

    assert engine.handle(message, now_ms=1)[0]["ok"] is True
    engine.handle(
        {
            "type": "action",
            "seq": 31,
            "action_id": "sim-map-action",
            "name": "move_cell",
            "target_ticks": 100,
        },
        now_ms=2,
    )
    busy = engine.handle(load_message(seq=32, version_id="map-v4"), now_ms=3)
    assert busy[0]["ok"] is False
    assert "active" in busy[0]["message"]


def test_sim_engine_refuses_start_when_requested_digest_is_not_loaded():
    engine = MazeSimEngine()
    message = load_message()
    engine.handle(message, now_ms=0)

    rejected = engine.handle(
        {
            "type": "start",
            "seq": 40,
            "map_version_id": "map-v3",
            "digest": "f" * 64,
        },
        now_ms=1,
    )
    accepted = engine.handle(
        {
            "type": "start",
            "seq": 41,
            "map_version_id": "map-v3",
            "digest": message["digest"],
        },
        now_ms=2,
    )

    assert rejected[0]["ok"] is False
    assert accepted[0]["ok"] is True
    assert accepted[0]["map_version_id"] == "map-v3"
    assert accepted[0]["digest"] == message["digest"]


def test_simulation_adapter_loads_exact_saved_version_before_reset_and_start():
    definition = validate_map_definition(definition_payload())

    class Version:
        version_id = "map-v3"
        digest = definition.content_digest

        @staticmethod
        def to_dict():
            return {
                "version_id": "map-v3",
                "digest": definition.content_digest,
                "definition": definition.to_dict(),
            }

    class Session:
        def __init__(self):
            self.calls = []

        def start(self):
            return None

        def wait_ready(self, *, timeout_s=None):
            return {"type": "ready", "fw": "maze-webots-sim"}

        def request_ack(self, message_type, **fields):
            self.calls.append((message_type, fields))
            return {
                "type": "ack",
                "seq": len(self.calls),
                "ok": True,
                "map_version_id": fields.get("map_version_id"),
                "digest": fields.get("digest"),
            }

        def snapshot(self):
            return {
                "connected": True,
                "ready": None,
                "telemetry": None,
                "last_error": None,
            }

        def stop(self):
            return self.request_ack("stop")

        def estop(self, *, reason):
            return self.request_ack("estop", reason=reason)

        def close(self):
            return None

    session = Session()
    adapter = SimulationModeAdapter(
        session_factory=lambda _endpoint: session,
        map_provider=lambda version_id: (
            Version() if version_id == "map-v3" else None
        ),
    )

    adapter.preflight()
    reset = adapter.reset(
        map_version="map-v3",
        param_version="param-v1",
    )
    started = adapter.start()

    assert reset["ack"]["map_version_id"] == "map-v3"
    assert started["ack"]["digest"] == definition.content_digest
    assert session.calls == [
        (
            "load_map",
            {
                "map_version_id": "map-v3",
                "digest": definition.content_digest,
                "definition": definition.to_dict(),
            },
        ),
        (
            "reset",
            {
                "map_version_id": "map-v3",
                "digest": definition.content_digest,
                "param_version": "param-v1",
            },
        ),
        (
            "start",
            {
                "map_version_id": "map-v3",
                "digest": definition.content_digest,
            },
        ),
    ]
