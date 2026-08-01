from collections import deque
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rdk_maze_tuner.core.device_session import DeviceSession
from rdk_maze_tuner.core.serial_client import SerialClient
from rdk_maze_tuner.dashboard.app import build_parser, create_app
from rdk_maze_tuner.dashboard.runtime import SerialDashboardRuntime
from rdk_maze_tuner.dashboard.state import DashboardState
from rdk_maze_tuner.core.param_manager import ParamManager
from rdk_maze_tuner.platform.auth import AuthService
from rdk_maze_tuner.platform.database import Database


PARAMS_PATH = Path("rdk_maze_tuner/config/params.yaml")
LIMITS_PATH = Path("rdk_maze_tuner/config/limits.yaml")
TEST_PASSWORD = "correct horse battery staple"


class FakeDashboardClient:
    def __init__(self, messages=None):
        self.messages = deque(messages or [])
        self.heartbeats = []
        self.actions = []
        self.started = False
        self.closed = False
        self.action_result = {
            "type": "done",
            "action_id": "dashboard-0001",
            "name": "move_cell",
            "success": True,
            "duration_ms": 2100,
            "enc_left": 1351,
            "enc_right": 1348,
        }
        self.action_ack = {"type": "ack", "seq": 99, "ok": True}

    @property
    def connected(self):
        return not self.closed

    def start(self):
        self.started = True

    def subscribe(self, *, message_types=None):
        return FakeSubscription(self.messages, message_types=message_types)

    def close(self):
        self.closed = True

    def send_heartbeat(self, *, ts_ms=None):
        self.heartbeats.append(ts_ms)
        return {"type": "ack", "seq": len(self.heartbeats), "ok": True}

    def execute_action_with_ack(self, *, action_id, name, speed, target_ticks):
        self.actions.append(
            {
                "action_id": action_id,
                "name": name,
                "speed": speed,
                "target_ticks": target_ticks,
            }
        )
        result = dict(self.action_result)
        result["action_id"] = action_id
        result["name"] = name
        return self.action_ack, result


class FakeSubscription:
    def __init__(self, messages, *, message_types=None):
        self.messages = messages
        self.message_types = (
            None if message_types is None else frozenset(message_types)
        )
        self.closed = False

    def get(self, *, timeout_s=0.0):
        while self.messages:
            message = self.messages.popleft()
            if (
                self.message_types is None
                or message.get("type") in self.message_types
            ):
                return message
        return None

    def close(self):
        self.closed = True


class BlockingDashboardClient(FakeDashboardClient):
    def __init__(self):
        super().__init__()
        self.action_started = threading.Event()
        self.release_action = threading.Event()

    def execute_action_with_ack(
        self,
        *,
        action_id,
        name,
        speed,
        target_ticks,
    ):
        self.action_started.set()
        assert self.release_action.wait(timeout=1.0)
        return super().execute_action_with_ack(
            action_id=action_id,
            name=name,
            speed=speed,
            target_ticks=target_ticks,
        )


class PassiveSerial:
    def write(self, data):
        return len(data)

    def flush(self):
        return None

    def readline(self):
        return b""


def make_state(client=None):
    return DashboardState(
        params=ParamManager(params_path=PARAMS_PATH, limits_path=LIMITS_PATH),
        client=client,
        clock_ms=lambda: 123456,
    )


def make_authenticated_client(tmp_path):
    from argon2 import PasswordHasher

    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()
    auth = AuthService(
        database=database,
        password_hasher=PasswordHasher(
            time_cost=1,
            memory_cost=8_192,
            parallelism=1,
        ),
    )
    auth.create_user("operator-a", TEST_PASSWORD)
    client = TestClient(
        create_app(database=database, auth_service=auth),
        base_url="https://testserver",
    )
    login = client.post(
        "/api/auth/login",
        json={"username": "operator-a", "password": TEST_PASSWORD},
    )
    csrf_token = login.json()["csrf_token"]
    claim = client.post(
        "/api/control/claim",
        headers={"X-CSRF-Token": csrf_token},
    )
    client.headers.update(
        {
            "X-CSRF-Token": csrf_token,
            "X-Control-Lease": claim.json()["lease_token"],
        }
    )
    return client


def make_test_app(tmp_path, *, client=None):
    from argon2 import PasswordHasher

    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()
    auth = AuthService(
        database=database,
        password_hasher=PasswordHasher(
            time_cost=1,
            memory_cost=8_192,
            parallelism=1,
        ),
    )
    return create_app(
        database=database,
        auth_service=auth,
        client=client,
    )


def test_dashboard_serves_workspace_with_estop_control(tmp_path):
    client = TestClient(make_test_app(tmp_path))

    response = client.get("/")

    assert response.status_code == 200
    assert 'id="estopButton"' in response.text
    assert 'id="mazeMap"' in response.text


def test_dashboard_state_contains_params_map_and_logs(tmp_path):
    client = make_authenticated_client(tmp_path)

    response = client.get("/api/state")

    assert response.status_code == 200
    payload = response.json()
    assert payload["connected"] is False
    assert payload["telemetry"]["state"] == "OFFLINE"
    assert payload["params"]["esp32_params"]["base_speed"] == 0.25
    assert payload["maze"]["position"] == [0, 0]
    assert payload["auto_tune_enabled"] is True
    assert isinstance(payload["logs"], list)


def test_dashboard_wraps_raw_serial_client_in_single_reader_session(tmp_path):
    raw_client = SerialClient(PassiveSerial(), timeout_s=0.01)

    app = make_test_app(tmp_path, client=raw_client)

    assert isinstance(app.state.dashboard.client, DeviceSession)
    assert app.state.dashboard.client.client is raw_client


def test_dashboard_param_update_validates_and_records_change(tmp_path):
    client = make_authenticated_client(tmp_path)

    response = client.post("/api/params", json={"updates": {"motor.base_speed": 0.26}})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["param_event"]["changes"] == {"motor.base_speed": [0.25, 0.26]}
    state = client.get("/api/state").json()
    assert state["params"]["esp32_params"]["base_speed"] == 0.26
    assert any(row["type"] == "param_change" for row in state["logs"])


def test_dashboard_rejects_out_of_range_param_update(tmp_path):
    client = make_authenticated_client(tmp_path)

    response = client.post("/api/params", json={"updates": {"motor.base_speed": 9.0}})

    assert response.status_code == 400
    assert "motor.base_speed" in response.json()["detail"]


def test_dashboard_estop_records_command_without_hardware(tmp_path):
    client = make_authenticated_client(tmp_path)

    response = client.post("/api/command/estop", json={"reason": "test"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["command"] == "estop"
    assert payload["sent_to_esp32"] is False
    state = client.get("/api/state").json()
    assert state["logs"][-1]["type"] == "command"
    assert state["logs"][-1]["payload"]["name"] == "estop"


def test_dashboard_websocket_sends_initial_state_snapshot(tmp_path):
    client = make_authenticated_client(tmp_path)

    with client.websocket_connect("wss://testserver/ws") as websocket:
        payload = websocket.receive_json()

    assert payload["type"] == "state"
    assert payload["payload"]["connected"] is False
    assert payload["payload"]["maze"]["position"] == [0, 0]


def test_serial_runtime_poll_once_uses_subscription_for_status_messages():
    fake_client = FakeDashboardClient(
        [
            {"type": "telemetry", "state": "IDLE", "front_mm": 240, "left_mm": 180, "right_mm": 300},
            {"type": "ready", "fw": "maze-esp32", "version": "0.1.0"},
        ]
    )
    state = make_state(client=fake_client)
    runtime = SerialDashboardRuntime(state=state)

    assert runtime.poll_once()["type"] == "telemetry"
    assert runtime.poll_once()["type"] == "ready"

    snapshot = state.snapshot()
    assert snapshot["telemetry"]["front_mm"] == 240
    assert snapshot["logs"][-2]["type"] == "telemetry"
    assert snapshot["logs"][-1]["type"] == "ready"


def test_serial_runtime_heartbeat_once_records_ack():
    fake_client = FakeDashboardClient()
    state = make_state(client=fake_client)
    runtime = SerialDashboardRuntime(state=state)

    response = runtime.send_heartbeat_once()

    assert response["ok"] is True
    assert fake_client.heartbeats == [123456]
    assert state.snapshot()["last_ack"]["seq"] == 1
    assert state.snapshot()["logs"][-1]["type"] == "heartbeat"


def test_dashboard_action_wait_does_not_block_heartbeat():
    fake_client = BlockingDashboardClient()
    state = make_state(client=fake_client)
    action_thread = threading.Thread(
        target=lambda: state.manual_action(name="move_cell")
    )
    action_thread.start()
    assert fake_client.action_started.wait(timeout=0.5)

    heartbeat_result = {}
    heartbeat_thread = threading.Thread(
        target=lambda: heartbeat_result.setdefault(
            "value",
            state.send_heartbeat(),
        )
    )
    heartbeat_thread.start()
    heartbeat_thread.join(timeout=0.2)

    try:
        assert heartbeat_thread.is_alive() is False
        assert heartbeat_result["value"]["ok"] is True
    finally:
        fake_client.release_action.set()
        action_thread.join(timeout=1.0)


def test_dashboard_frontend_uses_websocket_ping_refresh(tmp_path):
    client = TestClient(make_test_app(tmp_path))

    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert 'socket.send(JSON.stringify({ type: "ping" }))' in response.text


def test_dashboard_manual_move_sends_action_and_applies_done_to_map():
    fake_client = FakeDashboardClient()
    state = make_state(client=fake_client)

    response = state.manual_action(name="move_cell")

    assert response["ok"] is True
    assert response["sent_to_esp32"] is True
    assert response["action_id"] == "dashboard-0001"
    assert response["ack"]["ok"] is True
    assert response["result"]["type"] == "done"
    assert fake_client.actions == [
        {
            "action_id": "dashboard-0001",
            "name": "move_cell",
            "speed": 0.25,
            "target_ticks": 1350,
        }
    ]
    snapshot = state.snapshot()
    assert snapshot["maze"]["position"] == [0, 1]
    assert snapshot["current_action"]["type"] == "done"
    assert [row["type"] for row in snapshot["logs"][-3:]] == ["planned_action", "ack", "done"]


def test_dashboard_manual_turn_uses_turn_ticks_and_updates_heading():
    fake_client = FakeDashboardClient()
    fake_client.action_result = {"type": "done", "success": True, "duration_ms": 900}
    state = make_state(client=fake_client)

    response = state.manual_action(name="turn_right")

    assert response["action_id"] == "dashboard-0001"
    assert fake_client.actions[0]["speed"] == 0.18
    assert fake_client.actions[0]["target_ticks"] == 720
    assert state.snapshot()["maze"]["heading"] == "E"


def test_dashboard_manual_action_records_error_without_advancing_map():
    fake_client = FakeDashboardClient()
    fake_client.action_result = {
        "type": "error",
        "code": "OBSTACLE_TOO_CLOSE",
        "message": "front distance below danger_stop_mm",
        "front_mm": 55,
    }
    state = make_state(client=fake_client)

    response = state.manual_action(name="move_cell")

    assert response["ok"] is False
    assert response["result"]["type"] == "error"
    snapshot = state.snapshot()
    assert snapshot["maze"]["position"] == [0, 0]
    assert snapshot["current_action"]["type"] == "error"
    assert snapshot["logs"][-1]["type"] == "error"


def test_dashboard_parser_accepts_serial_or_tcp_but_not_both():
    parser = build_parser()

    assert parser.parse_args(["--serial", "/dev/ttyUSB0"]).serial == "/dev/ttyUSB0"
    assert parser.parse_args(["--tcp", "127.0.0.1:8765"]).tcp == "127.0.0.1:8765"
    assert parser.parse_args([]).serial is None
    with pytest.raises(SystemExit):
        parser.parse_args(["--serial", "/dev/ttyUSB0", "--tcp", "127.0.0.1:8765"])
