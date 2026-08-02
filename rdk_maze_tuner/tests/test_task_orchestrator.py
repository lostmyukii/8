import json
import threading
import time
from collections import deque
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from rdk_maze_tuner.core.device_session import DeviceDisconnectedError
from rdk_maze_tuner.core.maze_map import PlannedAction
from rdk_maze_tuner.core.maze_map import MazeMap
from rdk_maze_tuner.core.maze_planner import MazePlanner
from rdk_maze_tuner.core.maze_runner import MazeRunner, MazeStepResult
from rdk_maze_tuner.core.param_manager import ParamManager
from rdk_maze_tuner.dashboard.app import create_app
from rdk_maze_tuner.platform.auth import AuthService
from rdk_maze_tuner.platform.control_lease import ControlLeaseService
from rdk_maze_tuner.platform.database import Database
from rdk_maze_tuner.platform.event_store import EventStore
from rdk_maze_tuner.platform.physical_profile_repository import (
    PhysicalProfileRepository,
)
from rdk_maze_tuner.platform.task_orchestrator import (
    TaskConflictError,
    TaskOrchestrator,
)
from rdk_maze_tuner.platform.task_state import TaskStatus
from rdk_maze_tuner.platform.modes import (
    ModeAdapterError,
    SimulationModeAdapter,
)
from simulation.webots.maze_car.controllers.maze_sim_controller.sim_engine import (
    MazeSimEngine,
)
from simulation.webots.maze_car.controllers.maze_sim_controller.sim_server import (
    SimProtocolServer,
)


TEST_PASSWORD = "correct horse battery staple"


class FakeModeAdapter:
    def __init__(self, mode="simulation"):
        self.mode = mode
        self.calls = []
        self.connected = False

    def preflight(self, **context):
        self.calls.append(("preflight", context))
        self.connected = True
        return {
            "ok": True,
            "mode": self.mode,
            "code": "READY",
            "controller_version": "0.2.0",
            "webots_version": "R2025a",
        }

    def reset(
        self,
        *,
        map_version,
        param_version,
        physical_profile=None,
    ):
        self.calls.append(
            (
                "reset",
                {
                    "map_version": map_version,
                    "param_version": param_version,
                    "physical_profile": physical_profile,
                },
            )
        )
        return {"ok": True, "command": "reset"}

    def start(self):
        self.calls.append(("start", {}))
        return {"ok": True, "command": "start"}

    def pause(self):
        self.calls.append(("pause", {}))
        return {"ok": True, "command": "pause"}

    def stop(self):
        self.calls.append(("stop", {}))
        return {"ok": True, "command": "stop"}

    def estop(self):
        self.calls.append(("estop", {}))
        return {"ok": True, "command": "estop"}

    def clear_estop(self):
        self.calls.append(("clear_estop", {}))
        return {"ok": True, "command": "clear_estop"}

    def snapshot(self):
        return {
            "mode": self.mode,
            "connected": self.connected,
            "status": "ONLINE" if self.connected else "OFFLINE",
        }

    def close(self):
        self.calls.append(("close", {}))


def step_result(outcome):
    return MazeStepResult(
        action=PlannedAction("stop" if outcome != "continue" else "move_cell"),
        action_id=None,
        telemetry={"state": "IDLE"},
        done=None,
        map_text="+---+",
        outcome=outcome,
    )


class ScriptedRunner:
    def __init__(self, outcomes):
        self.outcomes = deque(outcomes)
        self.calls = 0

    def run_step(self, *, control, goal, event_sink):
        self.calls += 1
        event_sink(
            {
                "type": "scripted_step",
                "payload": {"index": self.calls},
            }
        )
        return step_result(self.outcomes.popleft())


class BlockingRunner:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def run_step(self, *, control, goal, event_sink):
        self.started.set()
        assert self.release.wait(timeout=1.0)
        if control.stop_requested():
            return step_result("stopped")
        if control.pause_requested():
            return step_result("paused")
        return step_result("continue")


class DisconnectingRunner:
    def run_step(self, *, control, goal, event_sink):
        raise DeviceDisconnectedError("device link lost")


def make_orchestrator(tmp_path, runners, *, run_finalizer=None):
    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()
    physical_profiles = PhysicalProfileRepository(database=database)
    physical_profiles.sync_from_yaml()
    events = EventStore(
        database=database,
        runs_dir=tmp_path / "runs",
    )
    adapter = FakeModeAdapter()
    runner_queue = deque(runners)
    task_ids = iter(("task-0001", "task-0002"))
    run_ids = iter(("run-0001", "run-0002", "run-0003"))
    orchestrator = TaskOrchestrator(
        database=database,
        event_store=events,
        adapters={"simulation": adapter},
        runner_factory=lambda _task: runner_queue.popleft(),
        task_id_factory=lambda: next(task_ids),
        run_id_factory=lambda: next(run_ids),
        utc_now=lambda: datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
        run_finalizer=run_finalizer,
        physical_profile_repository=physical_profiles,
    )
    return database, events, adapter, orchestrator


def create_ready_task(orchestrator):
    task = orchestrator.create_task(
        run_kind="exploration_complete",
        mode="simulation",
        map_version="map-v1",
        param_version="param-v1",
        goal={"type": "cell", "cell": [0, 1]},
        max_steps=20,
    )
    orchestrator.preflight(task["task_id"])
    return orchestrator.reset(task["task_id"])


def test_orchestrator_runs_to_goal_and_persists_structured_events(tmp_path):
    database, events, adapter, orchestrator = make_orchestrator(
        tmp_path,
        [ScriptedRunner(["continue", "goal_reached"])],
    )
    ready = create_ready_task(orchestrator)

    started = orchestrator.start(ready["task_id"])
    completed = orchestrator.wait_for_state(
        ready["task_id"],
        {TaskStatus.COMPLETED},
        timeout_s=1.0,
    )

    assert started["status"] == "RUNNING"
    assert completed["status"] == "COMPLETED"
    assert completed["step_count"] == 2
    assert any(
        event["type"] == "task.completed"
        for event in completed["recent_events"]
    )
    event_types = [
        event["type"] for event in events.list_events(ready["run_id"])
    ]
    assert "task.started" in event_types
    assert event_types.count("scripted_step") == 2
    assert "task.completed" in event_types
    assert ("stop", {}) in adapter.calls
    with database.connection() as connection:
        row = connection.execute(
            "SELECT status, started_at_utc, ended_at_utc FROM runs WHERE id = ?",
            (ready["run_id"],),
        ).fetchone()
    assert row["status"] == "COMPLETED"
    assert row["started_at_utc"] is not None
    assert row["ended_at_utc"] is not None


def test_simulation_task_defaults_profile_and_run_freezes_exact_snapshot(
    tmp_path,
):
    database, events, adapter, orchestrator = make_orchestrator(
        tmp_path,
        [ScriptedRunner(["goal_reached"])],
    )
    created = orchestrator.create_task(
        run_kind="exploration_complete",
        mode="simulation",
        map_version="map-v1",
        param_version="param-v1",
        goal={"type": "cell", "cell": [0, 1]},
    )

    assert created["physical_profile_id"] == "normal-v1"
    assert created["physical_profile_digest"]
    assert created["random_seed"] == 20260801
    preflight = orchestrator.preflight(created["task_id"])
    ready = orchestrator.reset(created["task_id"])

    assert preflight["preflight"]["controller_version"] == "0.2.0"
    assert ready["physical_profile_snapshot"]["profile_id"] == "normal-v1"
    assert adapter.calls[0][1]["physical_profile_id"] == "normal-v1"
    assert adapter.calls[1][1]["physical_profile"]["digest"] == (
        created["physical_profile_digest"]
    )
    with database.connection() as connection:
        row = connection.execute(
            """
            SELECT
                physical_profile_id, physical_profile_digest,
                physical_profile_snapshot_json, random_seed,
                controller_version, webots_version
            FROM runs
            WHERE id = ?
            """,
            (ready["run_id"],),
        ).fetchone()
    assert row["physical_profile_id"] == "normal-v1"
    assert row["physical_profile_digest"] == created["physical_profile_digest"]
    assert json.loads(row["physical_profile_snapshot_json"])[
        "profile_id"
    ] == "normal-v1"
    assert row["random_seed"] == 20260801
    assert row["controller_version"] == "0.2.0"
    assert row["webots_version"] == "R2025a"


def test_unsafe_physical_map_stays_at_preflight_without_creating_run(
    tmp_path,
):
    database, _events, adapter, orchestrator = make_orchestrator(
        tmp_path,
        [ScriptedRunner(["goal_reached"])],
    )
    adapter.preflight = lambda **_context: {
        "ok": False,
        "mode": "simulation",
        "code": "MAP_GEOMETRY_UNSAFE",
        "message": "physical passage is unsafe",
        "physical_preflight": {
            "actual_passage_x_mm": 280.0,
            "actual_passage_y_mm": 280.0,
        },
    }
    created = orchestrator.create_task(
        run_kind="exploration_complete",
        mode="simulation",
        map_version="unsafe-v1",
        param_version="param-v1",
        goal={"type": "cell", "cell": [0, 1]},
    )

    blocked = orchestrator.preflight(created["task_id"])

    assert blocked["status"] == "PREFLIGHT"
    assert blocked["preflight"]["code"] == "MAP_GEOMETRY_UNSAFE"
    assert blocked["run_id"] is None
    with pytest.raises(
        TaskConflictError,
        match="reset requires a successful PREFLIGHT",
    ):
        orchestrator.reset(created["task_id"])
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM runs"
        ).fetchone()[0] == 0


def test_completed_task_records_media_incomplete_archive_without_failing(
    tmp_path,
):
    finalized = []

    def finalize(run_id):
        finalized.append(run_id)
        return {
            "run_id": run_id,
            "status": "media_incomplete",
            "media_complete": False,
        }

    _, _, _, orchestrator = make_orchestrator(
        tmp_path,
        [ScriptedRunner(["goal_reached"])],
        run_finalizer=finalize,
    )
    ready = create_ready_task(orchestrator)

    orchestrator.start(ready["task_id"])
    completed = orchestrator.wait_for_state(
        ready["task_id"],
        {TaskStatus.COMPLETED},
        timeout_s=1.0,
    )

    assert completed["status"] == "COMPLETED"
    assert completed["archive_status"] == "media_incomplete"
    assert completed["archive"]["media_complete"] is False
    assert completed["archive_error"] is None
    assert finalized == [ready["run_id"]]


def test_archive_failure_is_recorded_but_does_not_fail_task(tmp_path):
    def finalize(_run_id):
        raise RuntimeError("ffmpeg unavailable")

    database, _, _, orchestrator = make_orchestrator(
        tmp_path,
        [ScriptedRunner(["goal_reached"])],
        run_finalizer=finalize,
    )
    ready = create_ready_task(orchestrator)

    orchestrator.start(ready["task_id"])
    completed = orchestrator.wait_for_state(
        ready["task_id"],
        {TaskStatus.COMPLETED},
        timeout_s=1.0,
    )

    assert completed["status"] == "COMPLETED"
    assert completed["archive_status"] == "incomplete"
    assert completed["archive_error"] == "ffmpeg unavailable"
    with database.connection() as connection:
        row = connection.execute(
            "SELECT metadata_json FROM runs WHERE id = ?",
            (ready["run_id"],),
        ).fetchone()
    assert json.loads(row["metadata_json"])["archive"]["status"] == "incomplete"


def test_pause_waits_for_step_boundary_and_then_stops_safely(tmp_path):
    runner = BlockingRunner()
    _, _, adapter, orchestrator = make_orchestrator(tmp_path, [runner])
    ready = create_ready_task(orchestrator)
    orchestrator.start(ready["task_id"])
    assert runner.started.wait(timeout=0.5)

    pausing = orchestrator.pause(ready["task_id"])
    assert pausing["status"] == "PAUSING"
    runner.release.set()
    paused = orchestrator.wait_for_state(
        ready["task_id"],
        {TaskStatus.PAUSED},
        timeout_s=1.0,
    )

    assert paused["status"] == "PAUSED"
    assert ("pause", {}) in adapter.calls


def test_late_step_result_cannot_mutate_a_stopped_run(tmp_path):
    runner = BlockingRunner()
    _, _, _, orchestrator = make_orchestrator(tmp_path, [runner])
    ready = create_ready_task(orchestrator)
    orchestrator.start(ready["task_id"])
    assert runner.started.wait(timeout=0.5)

    stopped = orchestrator.stop(ready["task_id"])
    runner.release.set()
    time.sleep(0.02)
    final = orchestrator.snapshot(ready["task_id"])

    assert stopped["status"] == "COMPLETED"
    assert final["status"] == "COMPLETED"
    assert final["step_count"] == 0
    assert final["last_step"] is None


def test_estop_locks_task_and_prevents_automatic_resume(tmp_path):
    runner = BlockingRunner()
    _, _, adapter, orchestrator = make_orchestrator(tmp_path, [runner])
    ready = create_ready_task(orchestrator)
    orchestrator.start(ready["task_id"])
    assert runner.started.wait(timeout=0.5)

    estopped = orchestrator.estop(ready["task_id"])

    try:
        assert estopped["status"] == "ESTOP"
        assert ("estop", {}) in adapter.calls
        with pytest.raises(TaskConflictError):
            orchestrator.start(ready["task_id"])
    finally:
        runner.release.set()


def test_only_one_task_can_hold_active_command_ownership(tmp_path):
    first_runner = BlockingRunner()
    _, _, _, orchestrator = make_orchestrator(
        tmp_path,
        [first_runner, ScriptedRunner(["goal_reached"])],
    )
    first = create_ready_task(orchestrator)
    second = create_ready_task(orchestrator)
    orchestrator.start(first["task_id"])
    assert first_runner.started.wait(timeout=0.5)

    try:
        with pytest.raises(TaskConflictError, match="another task is active"):
            orchestrator.start(second["task_id"])
    finally:
        orchestrator.estop(first["task_id"])
        first_runner.release.set()


def test_reset_after_completion_creates_a_new_run_id(tmp_path):
    _, _, _, orchestrator = make_orchestrator(
        tmp_path,
        [
            ScriptedRunner(["goal_reached"]),
            ScriptedRunner(["goal_reached"]),
        ],
    )
    first = create_ready_task(orchestrator)
    orchestrator.start(first["task_id"])
    orchestrator.wait_for_state(
        first["task_id"],
        {TaskStatus.COMPLETED},
        timeout_s=1.0,
    )

    orchestrator.preflight(first["task_id"])
    second = orchestrator.reset(first["task_id"])

    assert first["run_id"] == "run-0001"
    assert second["run_id"] == "run-0002"
    assert second["status"] == "READY"


def test_exhausted_planner_becomes_explicit_no_path_error(tmp_path):
    _, events, _, orchestrator = make_orchestrator(
        tmp_path,
        [ScriptedRunner(["exhausted"])],
    )
    ready = create_ready_task(orchestrator)

    orchestrator.start(ready["task_id"])
    failed = orchestrator.wait_for_state(
        ready["task_id"],
        {TaskStatus.ERROR},
        timeout_s=1.0,
    )

    assert failed["reason"] == "planner exhausted before reaching goal"
    error = next(
        event
        for event in events.list_events(ready["run_id"])
        if event["type"] == "task.error"
    )
    assert error["payload"]["code"] == "NO_PATH"


def test_running_device_disconnect_becomes_lost_not_error(tmp_path):
    _, events, _, orchestrator = make_orchestrator(
        tmp_path,
        [DisconnectingRunner()],
    )
    ready = create_ready_task(orchestrator)

    orchestrator.start(ready["task_id"])
    lost = orchestrator.wait_for_state(
        ready["task_id"],
        {TaskStatus.LOST},
        timeout_s=1.0,
    )

    assert lost["status"] == "LOST"
    assert lost["requires_manual_recovery"] is True
    assert lost["reason"] == "device link lost"
    assert any(
        event["type"] == "task.lost"
        for event in events.list_events(ready["run_id"])
    )


def test_deterministic_sim_engine_is_rejected_for_physical_task(tmp_path):
    engine = MazeSimEngine()
    server = SimProtocolServer(engine, port=0)
    port = server.listener.getsockname()[1]
    stopped = threading.Event()
    started_at = time.monotonic()

    def serve():
        while not stopped.is_set():
            server.poll(
                now_ms=int((time.monotonic() - started_at) * 1000)
            )
            time.sleep(0.002)

    server_thread = threading.Thread(target=serve, daemon=True)
    server_thread.start()
    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()
    events = EventStore(database=database, runs_dir=tmp_path / "runs")
    adapter = SimulationModeAdapter(
        endpoint=f"127.0.0.1:{port}",
        ready_timeout_s=1.0,
    )
    params = ParamManager(
        params_path=__import__("pathlib").Path(
            "rdk_maze_tuner/config/params.yaml"
        ),
        limits_path=__import__("pathlib").Path(
            "rdk_maze_tuner/config/limits.yaml"
        ),
    )

    def runner_factory(task):
        maze = MazeMap(
            wall_threshold_mm=int(params.get("tof.wall_threshold_mm"))
        )
        return MazeRunner(
            client=adapter.session,
            params=params,
            maze=maze,
            planner=MazePlanner(),
            action_prefix=task.run_id,
        )

    orchestrator = TaskOrchestrator(
        database=database,
        event_store=events,
        adapters={"simulation": adapter},
        runner_factory=runner_factory,
        task_id_factory=lambda: "task-sim",
        run_id_factory=lambda: "run-sim",
    )
    try:
        task = orchestrator.create_task(
            run_kind="exploration_complete",
            mode="simulation",
            map_version="map-v1",
            param_version="param-v1",
            goal={"type": "cell", "cell": [0, 1]},
            max_steps=10,
        )
        with pytest.raises(
            ModeAdapterError,
            match="PHYSICAL_BACKEND_REQUIRED",
        ):
            orchestrator.preflight(task["task_id"])
        assert orchestrator.snapshot(task["task_id"])["status"] == "ERROR"
    finally:
        orchestrator.close()
        stopped.set()
        server_thread.join(timeout=1.0)
        server.close()


def make_http_services(tmp_path, orchestrator, database):
    from argon2 import PasswordHasher

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
    app = create_app(
        database=database,
        auth_service=auth,
        control_lease_service=leases,
        task_orchestrator=orchestrator,
    )
    return app


def login(client, username):
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": TEST_PASSWORD},
    )
    assert response.status_code == 200
    return response.json()["csrf_token"]


def test_task_api_uses_control_lease_but_estop_remains_shared(tmp_path):
    runner = BlockingRunner()
    database, _, _, orchestrator = make_orchestrator(
        tmp_path,
        [runner],
    )
    app = make_http_services(tmp_path, orchestrator, database)

    with (
        TestClient(app, base_url="https://testserver") as holder,
        TestClient(app, base_url="https://testserver") as viewer,
    ):
        holder_csrf = login(holder, "operator-a")
        viewer_csrf = login(viewer, "operator-b")
        claim = holder.post(
            "/api/control/claim",
            headers={"X-CSRF-Token": holder_csrf},
        )
        lease_token = claim.json()["lease_token"]
        holder.headers.update(
            {
                "X-CSRF-Token": holder_csrf,
                "X-Control-Lease": lease_token,
            }
        )

        denied = viewer.post(
            "/api/tasks",
            json={
                "run_kind": "exploration_complete",
                "mode": "simulation",
                "map_version": "map-v1",
                "param_version": "param-v1",
                "goal": {"type": "cell", "cell": [0, 1]},
            },
            headers={"X-CSRF-Token": viewer_csrf},
        )
        created = holder.post(
            "/api/tasks",
            json={
                "run_kind": "exploration_complete",
                "mode": "simulation",
                "map_version": "map-v1",
                "param_version": "param-v1",
                "goal": {"type": "cell", "cell": [0, 1]},
            },
        )
        task_id = created.json()["task_id"]
        preflight = holder.post(f"/api/tasks/{task_id}/preflight")
        reset = holder.post(f"/api/tasks/{task_id}/reset")
        started = holder.post(f"/api/tasks/{task_id}/start")
        assert runner.started.wait(timeout=0.5)
        pausing = holder.post(f"/api/tasks/{task_id}/pause")
        runner.release.set()
        orchestrator.wait_for_state(
            task_id,
            {TaskStatus.PAUSED},
            timeout_s=1.0,
        )
        stopped = holder.post(f"/api/tasks/{task_id}/stop")
        shared_estop = viewer.post(
            f"/api/tasks/{task_id}/estop",
            headers={"X-CSRF-Token": viewer_csrf},
        )

    assert denied.status_code == 403
    assert created.status_code == 201
    assert preflight.status_code == 200
    assert reset.status_code == 200
    assert started.status_code == 200
    assert started.json()["status"] == "RUNNING"
    assert pausing.status_code == 200
    assert pausing.json()["status"] == "PAUSING"
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "COMPLETED"
    assert shared_estop.status_code == 200
    assert shared_estop.json()["status"] == "ESTOP"


def test_legacy_commands_respect_active_task_command_ownership(tmp_path):
    runner = BlockingRunner()
    database, _, adapter, orchestrator = make_orchestrator(
        tmp_path,
        [runner],
    )
    app = make_http_services(tmp_path, orchestrator, database)

    with (
        TestClient(app, base_url="https://testserver") as holder,
        TestClient(app, base_url="https://testserver") as viewer,
    ):
        holder_csrf = login(holder, "operator-a")
        viewer_csrf = login(viewer, "operator-b")
        claim = holder.post(
            "/api/control/claim",
            headers={"X-CSRF-Token": holder_csrf},
        )
        holder.headers.update(
            {
                "X-CSRF-Token": holder_csrf,
                "X-Control-Lease": claim.json()["lease_token"],
            }
        )

        created = holder.post(
            "/api/tasks",
            json={
                "run_kind": "exploration_complete",
                "mode": "simulation",
                "map_version": "map-v1",
                "param_version": "param-v1",
                "goal": {"type": "cell", "cell": [0, 1]},
            },
        )
        task_id = created.json()["task_id"]
        holder.post(f"/api/tasks/{task_id}/preflight")
        holder.post(f"/api/tasks/{task_id}/reset")
        holder.post(f"/api/tasks/{task_id}/start")
        assert runner.started.wait(timeout=0.5)

        manual_action = holder.post(
            "/api/command/action",
            json={"name": "move_cell"},
        )
        global_estop = viewer.post(
            "/api/command/estop",
            json={"reason": "shared dashboard estop"},
            headers={"X-CSRF-Token": viewer_csrf},
        )
        runner.release.set()

    assert manual_action.status_code == 409
    assert "active task" in manual_action.json()["detail"]
    assert global_estop.status_code == 200
    assert global_estop.json()["routed_to"] == "task"
    assert global_estop.json()["task"]["status"] == "ESTOP"
    assert orchestrator.snapshot(task_id)["status"] == "ESTOP"
    assert adapter.calls.count(("estop", {})) == 1
