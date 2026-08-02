from __future__ import annotations

from collections import deque
from pathlib import Path

from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from rdk_maze_tuner.core.maze_map import MazeMap
from rdk_maze_tuner.core.param_manager import ParamManager
from rdk_maze_tuner.dashboard.app import create_app
from rdk_maze_tuner.dashboard.state import DashboardState
from rdk_maze_tuner.platform.auth import AuthService
from rdk_maze_tuner.platform.control_lease import ControlLeaseService
from rdk_maze_tuner.platform.database import Database
from rdk_maze_tuner.platform.map_repository import MapRepository


PARAMS = Path("rdk_maze_tuner/config/params.yaml")
LIMITS = Path("rdk_maze_tuner/config/limits.yaml")
TEST_PASSWORD = "correct horse battery staple"


def corridor_definition(*, blocked: bool = False) -> dict:
    walls = [
        {"x1": 0, "y1": 0, "x2": 1, "y2": 0},
        {"x1": 0, "y1": 2, "x2": 1, "y2": 2},
        {"x1": 0, "y1": 0, "x2": 0, "y2": 1},
        {"x1": 0, "y1": 1, "x2": 0, "y2": 2},
        {"x1": 1, "y1": 0, "x2": 1, "y2": 1},
        {"x1": 1, "y1": 1, "x2": 1, "y2": 2},
    ]
    if blocked:
        walls.append({"x1": 0, "y1": 1, "x2": 1, "y2": 1})
    return {
        "rows": 2,
        "cols": 1,
        "cell_width_mm": 450,
        "cell_height_mm": 450,
        "wall_thickness_mm": 40,
        "wall_height_mm": 180,
        "start": {"x": 0, "y": 1, "heading": "N"},
        "goals": [
            {"x": 0, "y": 1 if blocked else 0},
        ],
        "walls": walls,
    }


class CompletionSubscription:
    def __init__(self, messages):
        self.messages = deque(messages)
        self.closed = False

    def get(self, *, timeout_s=0.0):
        return self.messages.popleft() if self.messages else None

    def close(self):
        self.closed = True


class DebugEvidenceClient:
    timeout_s = 0.05

    def __init__(self):
        self.executed = []
        self.estops = []
        self.done_ticks = 1350
        self.completion_front_mm = 225
        self.baseline = {
            "type": "telemetry",
            "ts_ms": 0,
            "enc_left": 0,
            "enc_right": 0,
            "front_mm": 675,
            "left_mm": 225,
            "right_mm": 225,
            "imu_available": True,
            "imu_yaw_deg": 0,
        }

    @property
    def connected(self):
        return True

    def wait_telemetry(self, *, timeout_s=None):
        return dict(self.baseline)

    def subscribe(self, *, message_types=None, max_queue=128):
        return CompletionSubscription(
            [
                {
                    "type": "telemetry",
                    "ts_ms": 1000,
                    "enc_left": self.done_ticks,
                    "enc_right": self.done_ticks,
                    "front_mm": self.completion_front_mm,
                    "left_mm": 225,
                    "right_mm": 225,
                    "imu_available": True,
                    "imu_yaw_deg": 0,
                }
            ]
        )

    def execute_action(
        self,
        *,
        action_id,
        name,
        speed,
        target_ticks,
        **_extra,
    ):
        self.executed.append(
            {
                "action_id": action_id,
                "name": name,
                "speed": speed,
                "target_ticks": target_ticks,
            }
        )
        return {
            "type": "done",
            "action_id": action_id,
            "name": name,
            "success": True,
            "duration_ms": 1000,
            "enc_left": self.done_ticks,
            "enc_right": self.done_ticks,
        }

    def estop(self, *, reason="dashboard"):
        self.estops.append(reason)
        return {"type": "ack", "seq": 1, "ok": True}

    def stop(self):
        return {"type": "ack", "seq": 2, "ok": True}


def make_services(tmp_path, *, blocked=False):
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
    auth.create_user("operator-b", TEST_PASSWORD)
    leases = ControlLeaseService(database=database)
    maps = MapRepository(
        database=database,
        artifacts_dir=tmp_path / "artifacts",
    )
    _map_record, version = maps.create_map(
        name="debug corridor",
        definition=corridor_definition(blocked=blocked),
        created_by_user_id=None,
    )
    maze = MazeMap.from_definition(
        version.definition,
        wall_threshold_mm=150,
        map_version_id=version.version_id,
    )
    evidence_client = DebugEvidenceClient()
    state = DashboardState(
        params=ParamManager(params_path=PARAMS, limits_path=LIMITS),
        maze=maze,
        client=evidence_client,
        clock_ms=lambda: 123456,
    )
    app = create_app(
        database=database,
        auth_service=auth,
        control_lease_service=leases,
        map_repository=maps,
        state=state,
    )
    return database, app, state, evidence_client, version


def login(client, username):
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": TEST_PASSWORD},
    )
    assert response.status_code == 200
    return response.json()["csrf_token"]


def claim(client, csrf):
    response = client.post(
        "/api/control/claim",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    client.headers.update(
        {
            "X-CSRF-Token": csrf,
            "X-Control-Lease": response.json()["lease_token"],
        }
    )


def request_body(version, *, execute=False, target=(0, 0)):
    return {
        "map_version": version.version_id,
        "target_cell": [target[0], target[1]],
        "execute": execute,
    }


def test_debug_api_requires_auth_csrf_and_control_lease_but_estop_is_shared(
    tmp_path,
):
    _database, app, _state, evidence_client, version = make_services(
        tmp_path
    )
    holder = TestClient(app, base_url="https://testserver")
    viewer = TestClient(app, base_url="https://testserver")

    assert holder.post(
        "/api/debug/step",
        json=request_body(version),
    ).status_code == 401
    holder_csrf = login(holder, "operator-a")
    assert holder.post(
        "/api/debug/step",
        json=request_body(version),
    ).status_code == 403
    assert holder.post(
        "/api/debug/step",
        json=request_body(version),
        headers={"X-CSRF-Token": holder_csrf},
    ).status_code == 403
    claim(holder, holder_csrf)
    assert holder.post(
        "/api/debug/step",
        json=request_body(version),
    ).status_code == 200

    viewer_csrf = login(viewer, "operator-b")
    assert viewer.post(
        "/api/debug/step",
        json=request_body(version),
        headers={"X-CSRF-Token": viewer_csrf},
    ).status_code == 403
    assert viewer.post(
        "/api/command/estop",
        json={"reason": "shared safety"},
        headers={"X-CSRF-Token": viewer_csrf},
    ).status_code == 200
    assert evidence_client.estops == ["shared safety"]


def test_debug_preview_rejects_out_of_bounds_and_unreachable_targets(
    tmp_path,
):
    _database, app, _state, evidence_client, version = make_services(
        tmp_path,
        blocked=True,
    )
    client = TestClient(app, base_url="https://testserver")
    csrf = login(client, "operator-a")
    claim(client, csrf)

    out_of_bounds = client.post(
        "/api/debug/step",
        json=request_body(version, target=(1, 0)),
    )
    unreachable = client.post(
        "/api/debug/step",
        json=request_body(version, target=(0, 0)),
    )

    assert out_of_bounds.status_code == 400
    assert out_of_bounds.json()["detail"]["code"] == (
        "DEBUG_TARGET_OUT_OF_BOUNDS"
    )
    assert unreachable.status_code == 400
    assert unreachable.json()["detail"]["code"] == "DEBUG_TARGET_UNREACHABLE"
    assert evidence_client.executed == []


def test_debug_preview_plans_one_action_without_starting_a_task(tmp_path):
    database, app, state, evidence_client, version = make_services(tmp_path)
    client = TestClient(app, base_url="https://testserver")
    csrf = login(client, "operator-a")
    claim(client, csrf)

    response = client.post(
        "/api/debug/step",
        json=request_body(version),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "preview"
    assert payload["executed"] is False
    assert payload["target_cell"] == [0, 0]
    assert payload["next_action"] == {
        "name": "move_cell",
        "direction": "N",
    }
    assert evidence_client.executed == []
    assert app.state.task_orchestrator.list_tasks() == []
    assert state.maze.position == (0, 1)
    assert state.logs[-1]["type"] == "debug.preview"
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_debug_execute_waits_for_one_action_and_never_completes_or_scores(
    tmp_path,
):
    database, app, state, evidence_client, version = make_services(tmp_path)
    client = TestClient(app, base_url="https://testserver")
    csrf = login(client, "operator-a")
    claim(client, csrf)

    response = client.post(
        "/api/debug/step",
        json=request_body(version, execute=True),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "execute"
    assert payload["executed"] is True
    assert payload["result"]["outcome"] == "continue"
    assert payload["result"]["evidence"]["status"] == "accepted"
    assert len(evidence_client.executed) == 1
    assert evidence_client.executed[0]["action_id"].startswith("debug-")
    assert state.maze.position == (0, 0)
    assert state.snapshot()["pose"]["grid_cell"] == [0, 0]
    assert app.state.task_orchestrator.list_tasks() == []
    assert all(
        row["type"].startswith("debug.")
        for row in state.logs
        if row["type"].startswith("debug.")
    )
    assert {
        "step.goal_verified",
        "task.completed",
    }.isdisjoint(row["type"] for row in state.logs)
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_debug_recoverable_evidence_never_sends_an_automatic_correction(
    tmp_path,
):
    _database, app, _state, evidence_client, version = make_services(
        tmp_path
    )
    evidence_client.done_ticks = 1200
    evidence_client.completion_front_mm = 300
    client = TestClient(app, base_url="https://testserver")
    csrf = login(client, "operator-a")
    claim(client, csrf)

    response = client.post(
        "/api/debug/step",
        json=request_body(version, execute=True),
    )

    assert response.status_code == 200
    assert response.json()["result"]["outcome"] == "unsafe"
    assert response.json()["result"]["evidence"]["status"] == "recoverable"
    assert len(evidence_client.executed) == 1
    assert evidence_client.executed[0]["name"] == "move_cell"


def test_debug_step_is_blocked_while_an_auto_task_owns_commands(tmp_path):
    _database, app, _state, evidence_client, version = make_services(tmp_path)
    client = TestClient(app, base_url="https://testserver")
    csrf = login(client, "operator-a")
    claim(client, csrf)
    app.state.task_orchestrator.command_owner = lambda: {
        "task_id": "task-running",
        "status": "RUNNING",
    }

    response = client.post(
        "/api/debug/step",
        json=request_body(version),
    )

    assert response.status_code == 409
    assert "task-running" in response.json()["detail"]
    assert evidence_client.executed == []
