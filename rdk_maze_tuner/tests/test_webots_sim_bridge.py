import threading
import time

from rdk_maze_tuner.core.device_session import DeviceSession
from rdk_maze_tuner.core.serial_client import SerialClient
from rdk_maze_tuner.core.tcp_stream import open_tcp
from simulation.webots.maze_car.controllers.maze_sim_controller.sim_engine import MazeSimEngine
from simulation.webots.maze_car.controllers.maze_sim_controller.sim_server import SimProtocolServer


def test_sim_engine_move_action_has_ack_telemetry_and_matching_done():
    engine = MazeSimEngine()

    replies = engine.handle(
        {
            "type": "action",
            "seq": 7,
            "action_id": "sim-0001",
            "name": "move_cell",
            "speed": 0.25,
            "target_ticks": 1350,
        },
        now_ms=100,
    )

    assert replies[0] == {"type": "ack", "seq": 7, "ok": True}
    assert replies[1]["state"] == "MOVING_CELL"
    assert engine.world_pose()[:2] == (-0.9, 0.9)

    completed = engine.tick(now_ms=800)

    done = next(message for message in completed if message["type"] == "done")
    assert done["action_id"] == "sim-0001"
    assert done["enc_left"] == 1350
    assert done["enc_right"] == 1350
    assert engine.cell == (0, 3)


def test_sim_engine_reports_obstacle_with_action_id():
    engine = MazeSimEngine()
    engine.cell = (0, 0)
    engine.heading_index = 0

    replies = engine.handle(
        {
            "type": "action",
            "seq": 2,
            "action_id": "blocked-0001",
            "name": "move_cell",
            "speed": 0.25,
            "target_ticks": 1350,
        },
        now_ms=0,
    )

    assert replies[0]["ok"] is True
    assert replies[1]["type"] == "error"
    assert replies[1]["action_id"] == "blocked-0001"
    assert replies[1]["code"] == "OBSTACLE_TOO_CLOSE"


def test_sim_engine_estop_cancels_active_action_and_rejects_new_actions():
    engine = MazeSimEngine()
    engine.handle(
        {
            "type": "action",
            "seq": 1,
            "action_id": "sim-0002",
            "name": "move_cell",
            "speed": 0.25,
            "target_ticks": 1350,
        },
        now_ms=0,
    )

    replies = engine.handle({"type": "estop", "seq": 2, "reason": "test"}, now_ms=10)

    assert replies[0] == {"type": "ack", "seq": 2, "ok": True}
    assert replies[1]["action_id"] == "sim-0002"
    assert replies[1]["code"] == "ESTOP"
    assert engine.telemetry_message()["state"] == "ESTOP"

    rejected = engine.handle(
        {
            "type": "action",
            "seq": 3,
            "action_id": "sim-0003",
            "name": "turn_left",
            "speed": 0.18,
            "target_ticks": 720,
        },
        now_ms=20,
    )
    assert rejected[0]["ok"] is False


def test_tcp_bridge_runs_existing_serial_client_contract():
    engine = MazeSimEngine()
    server = SimProtocolServer(engine, port=0)
    port = server.listener.getsockname()[1]
    stopped = threading.Event()
    started = time.monotonic()

    def serve():
        while not stopped.is_set():
            server.poll(now_ms=int((time.monotonic() - started) * 1000))
            time.sleep(0.002)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    stream = open_tcp(f"127.0.0.1:{port}", read_timeout_s=0.01)
    client = SerialClient(stream, timeout_s=2.0)
    try:
        assert client.wait_ready()["fw"] == "maze-webots-sim"
        assert client.send_params({"base_speed": 0.25})["ok"] is True
        done = client.execute_action(
            action_id="integration-0001",
            name="move_cell",
            speed=0.25,
            target_ticks=1350,
        )
        assert done["type"] == "done"
        assert done["action_id"] == "integration-0001"
        assert done["simulated"] is True
    finally:
        stream.close()
        stopped.set()
        thread.join(timeout=1.0)
        server.close()


def test_tcp_bridge_runs_through_single_reader_device_session():
    engine = MazeSimEngine()
    server = SimProtocolServer(engine, port=0)
    port = server.listener.getsockname()[1]
    stopped = threading.Event()
    started = time.monotonic()

    def serve():
        while not stopped.is_set():
            server.poll(now_ms=int((time.monotonic() - started) * 1000))
            time.sleep(0.002)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    stream = open_tcp(f"127.0.0.1:{port}", read_timeout_s=0.01)
    session = DeviceSession(SerialClient(stream, timeout_s=2.0))
    try:
        session.start()
        assert session.wait_ready()["fw"] == "maze-webots-sim"
        assert session.send_heartbeat(ts_ms=123456)["ok"] is True
        done = session.execute_action(
            action_id="session-integration-0001",
            name="move_cell",
            speed=0.25,
            target_ticks=1350,
        )
        assert done["type"] == "done"
        assert done["action_id"] == "session-integration-0001"
        assert session.last_telemetry["simulated"] is True
    finally:
        session.close()
        stopped.set()
        thread.join(timeout=1.0)
        server.close()


def test_deterministic_engine_connection_hooks_are_noop_and_close_is_safe():
    engine = MazeSimEngine()

    engine.on_client_connected(now_ms=10)
    engine.on_client_disconnected(now_ms=20)
    engine.close()

    assert engine.telemetry_message()["state"] == "IDLE"


def test_deterministic_engine_supports_bounded_recovery_actions_without_grid_commit():
    engine = MazeSimEngine()
    start_cell = engine.cell
    start_heading = engine.heading

    nudge = engine.handle(
        {
            "type": "action",
            "seq": 10,
            "action_id": "recovery-nudge",
            "name": "nudge_forward",
            "speed": 0.10,
            "target_ticks": 300,
            "recovery": True,
            "parent_action_id": "move-1",
        },
        now_ms=0,
    )
    assert nudge[0]["ok"] is True
    nudge_done = next(
        item
        for item in engine.tick(now_ms=800)
        if item["type"] == "done"
    )
    assert nudge_done["recovery"] is True
    assert engine.cell == start_cell

    align = engine.handle(
        {
            "type": "action",
            "seq": 11,
            "action_id": "recovery-align",
            "name": "align_heading",
            "direction": "left",
            "speed": 0.09,
            "target_ticks": 60,
            "recovery": True,
            "parent_action_id": "move-1",
        },
        now_ms=900,
    )
    assert align[0]["ok"] is True
    align_done = next(
        item
        for item in engine.tick(now_ms=1400)
        if item["type"] == "done"
    )
    assert align_done["direction"] == "left"
    assert engine.heading == start_heading


def test_deterministic_engine_rejects_unbounded_or_reused_recovery_action():
    engine = MazeSimEngine()
    invalid = engine.handle(
        {
            "type": "action",
            "seq": 12,
            "action_id": "bad-recovery",
            "name": "align_heading",
            "direction": "back",
            "speed": 0.09,
            "target_ticks": 60,
            "recovery": True,
        },
        now_ms=0,
    )
    assert invalid[0]["ok"] is False
    assert engine.pending is None

    valid = {
        "type": "action",
        "seq": 13,
        "action_id": "once-only",
        "name": "nudge_forward",
        "speed": 0.10,
        "target_ticks": 300,
        "recovery": True,
    }
    assert engine.handle(valid, now_ms=10)[0]["ok"] is True
    engine.tick(now_ms=800)
    reused = engine.handle({**valid, "seq": 14}, now_ms=900)
    assert reused[0]["ok"] is False
