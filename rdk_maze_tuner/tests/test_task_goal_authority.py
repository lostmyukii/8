from __future__ import annotations

import json

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from rdk_maze_tuner.dashboard.app import create_app
from rdk_maze_tuner.platform.auth import AuthService
from rdk_maze_tuner.platform.control_lease import ControlLeaseService
from rdk_maze_tuner.platform.database import Database
from rdk_maze_tuner.platform.event_store import EventStore
from rdk_maze_tuner.platform.map_goal_resolver import MapGoalResolver
from rdk_maze_tuner.platform.map_repository import MapRepository
from rdk_maze_tuner.platform.task_orchestrator import (
    TaskOrchestrator,
    TaskValidationError,
)


TEST_PASSWORD = "correct horse battery staple"


class ReadyAdapter:
    mode = "simulation"

    def __init__(self) -> None:
        self.connected = False

    def preflight(self, **_context):
        self.connected = True
        return {
            "ok": True,
            "mode": self.mode,
            "code": "READY",
            "controller_version": "0.2.0",
            "webots_version": "R2025a",
        }

    def reset(self, **_context):
        return {"ok": True, "command": "reset"}

    def snapshot(self):
        return {
            "mode": self.mode,
            "connected": self.connected,
            "status": "ONLINE" if self.connected else "OFFLINE",
        }

    def close(self):
        return None


def map_definition(*, goal: tuple[int, int]) -> dict:
    rows = 5
    cols = 5
    walls = [
        *(
            {"x1": x, "y1": 0, "x2": x + 1, "y2": 0}
            for x in range(cols)
        ),
        *(
            {"x1": x, "y1": rows, "x2": x + 1, "y2": rows}
            for x in range(cols)
        ),
        *(
            {"x1": 0, "y1": y, "x2": 0, "y2": y + 1}
            for y in range(rows)
        ),
        *(
            {"x1": cols, "y1": y, "x2": cols, "y2": y + 1}
            for y in range(rows)
        ),
    ]
    return {
        "rows": rows,
        "cols": cols,
        "cell_width_mm": 450,
        "cell_height_mm": 450,
        "wall_thickness_mm": 40,
        "wall_height_mm": 180,
        "start": {"x": 0, "y": 4, "heading": "N"},
        "goals": [{"x": goal[0], "y": goal[1]}],
        "walls": walls,
    }


def make_map_repository(tmp_path, database):
    identifiers = iter(("task12", "v1", "v2", "v3"))
    repository = MapRepository(
        database=database,
        artifacts_dir=tmp_path / "artifacts",
        id_factory=lambda: next(identifiers),
    )
    map_record, _v1 = repository.create_map(
        name="Task12 公网验收迷宫",
        definition=map_definition(goal=(0, 3)),
        created_by_user_id=None,
    )
    v2 = repository.save_version(
        map_id=map_record["map_id"],
        definition=map_definition(goal=(4, 0)),
        created_by_user_id=None,
    )
    return repository, map_record, v2


def make_orchestrator(tmp_path):
    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()
    maps, map_record, v2 = make_map_repository(tmp_path, database)
    events = EventStore(database=database, runs_dir=tmp_path / "runs")
    task_ids = iter(("task-0001", "task-0002", "task-0003"))
    run_ids = iter(("run-0001", "run-0002", "run-0003"))
    orchestrator = TaskOrchestrator(
        database=database,
        event_store=events,
        adapters={"simulation": ReadyAdapter()},
        runner_factory=lambda _task: object(),
        map_goal_resolver=MapGoalResolver(
            map_provider=maps.get_version,
        ),
        task_id_factory=lambda: next(task_ids),
        run_id_factory=lambda: next(run_ids),
    )
    return database, maps, map_record, v2, events, orchestrator


def test_auto_task_resolves_v2_goal_and_freezes_same_evidence_everywhere(
    tmp_path,
):
    database, _maps, _map_record, v2, events, orchestrator = (
        make_orchestrator(tmp_path)
    )

    created = orchestrator.create_task(
        mode="simulation",
        map_version=v2.version_id,
        param_version="param-v1",
    )

    expected_goal = {
        "type": "map_goal",
        "cell": [4, 0],
        "candidate_cells": [[4, 0]],
        "source_map_version": v2.version_id,
        "source_map_digest": v2.digest,
        "resolution": "single",
        "path_length_cells": 8,
    }
    assert created["run_kind"] == "auto_to_map_goal"
    assert created["goal"] == expected_goal
    assert created["recent_events"][0]["type"] == "task.created"
    assert created["recent_events"][0]["payload"]["run_kind"] == (
        "auto_to_map_goal"
    )
    assert created["recent_events"][0]["payload"]["goal"] == expected_goal

    orchestrator.preflight(created["task_id"])
    ready = orchestrator.reset(created["task_id"])
    with database.connection() as connection:
        row = connection.execute(
            "SELECT metadata_json FROM runs WHERE id = ?",
            (ready["run_id"],),
        ).fetchone()
    metadata = json.loads(row["metadata_json"])
    assert metadata["run_kind"] == "auto_to_map_goal"
    assert metadata["goal"] == expected_goal
    created_event = next(
        event
        for event in events.list_events(ready["run_id"])
        if event["type"] == "task.created"
    )
    assert created_event["payload"]["goal"] == expected_goal


def test_auto_goal_override_is_rejected_without_task_run_or_event(tmp_path):
    database, _maps, _map_record, v2, _events, orchestrator = (
        make_orchestrator(tmp_path)
    )

    with pytest.raises(TaskValidationError) as captured:
        orchestrator.create_task(
            mode="simulation",
            map_version=v2.version_id,
            param_version="param-v1",
            goal={"type": "cell", "cell": [0, 3]},
        )

    assert captured.value.code == "AUTO_GOAL_OVERRIDE_FORBIDDEN"
    assert orchestrator.list_tasks() == []
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_new_map_version_creates_new_goal_without_mutating_old_task(tmp_path):
    _database, maps, map_record, v2, _events, orchestrator = (
        make_orchestrator(tmp_path)
    )
    first = orchestrator.create_task(
        mode="simulation",
        map_version=v2.version_id,
        param_version="param-v1",
    )
    v3 = maps.save_version(
        map_id=map_record["map_id"],
        definition=map_definition(goal=(0, 0)),
        created_by_user_id=None,
    )

    second = orchestrator.create_task(
        mode="simulation",
        map_version=v3.version_id,
        param_version="param-v1",
    )

    assert first["goal"]["cell"] == [4, 0]
    assert second["goal"]["cell"] == [0, 0]
    assert orchestrator.snapshot(first["task_id"])["goal"] == first["goal"]


def test_exploration_complete_requires_explicit_internal_run_kind(tmp_path):
    _database, _maps, _map_record, _v2, _events, orchestrator = (
        make_orchestrator(tmp_path)
    )

    created = orchestrator.create_task(
        run_kind="exploration_complete",
        mode="simulation",
        map_version="internal-map-fixture",
        param_version="param-v1",
        goal={"type": "exploration_complete"},
    )

    assert created["run_kind"] == "exploration_complete"
    assert created["goal"] == {"type": "exploration_complete"}


def test_task_api_defaults_to_map_goal_and_rejects_cell_override(tmp_path):
    database, maps, _map_record, v2, _events, orchestrator = (
        make_orchestrator(tmp_path)
    )
    auth = AuthService(
        database=database,
        password_hasher=PasswordHasher(
            time_cost=1,
            memory_cost=8_192,
            parallelism=1,
        ),
    )
    auth.create_user("operator-a", TEST_PASSWORD)
    leases = ControlLeaseService(database=database)
    app = create_app(
        database=database,
        auth_service=auth,
        control_lease_service=leases,
        task_orchestrator=orchestrator,
        map_repository=maps,
    )

    with TestClient(app, base_url="https://testserver") as client:
        login = client.post(
            "/api/auth/login",
            json={
                "username": "operator-a",
                "password": TEST_PASSWORD,
            },
        )
        csrf = login.json()["csrf_token"]
        claim = client.post(
            "/api/control/claim",
            headers={"X-CSRF-Token": csrf},
        )
        client.headers.update(
            {
                "X-CSRF-Token": csrf,
                "X-Control-Lease": claim.json()["lease_token"],
            }
        )
        created = client.post(
            "/api/tasks",
            json={
                "mode": "simulation",
                "map_version": v2.version_id,
                "param_version": "param-v1",
            },
        )
        task_count = len(orchestrator.list_tasks())
        rejected = client.post(
            "/api/tasks",
            json={
                "mode": "simulation",
                "map_version": v2.version_id,
                "param_version": "param-v1",
                "goal": {"type": "cell", "cell": [0, 3]},
            },
        )

    assert created.status_code == 201
    assert created.json()["run_kind"] == "auto_to_map_goal"
    assert created.json()["goal"]["cell"] == [4, 0]
    assert rejected.status_code == 400
    assert rejected.json()["detail"]["code"] == (
        "AUTO_GOAL_OVERRIDE_FORBIDDEN"
    )
    assert len(orchestrator.list_tasks()) == task_count
