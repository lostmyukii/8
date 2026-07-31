import threading
import time

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
