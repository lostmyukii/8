from __future__ import annotations

import sqlite3

import pytest

from rdk_maze_tuner.platform.database import Database
from rdk_maze_tuner.platform.param_version_repository import (
    ParamPolicyError,
    ParamVersionRepository,
)


def repository(tmp_path):
    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()
    return database, ParamVersionRepository(database=database)


def base_snapshot():
    return {
        "motor": {"base_speed": 0.25, "max_pwm": 180},
        "tof": {"front_stop_mm": 120, "danger_stop_mm": 60},
        "safety": {
            "heartbeat_timeout_ms": 500,
            "action_timeout_ms": 8000,
        },
        "arrival_verification": {
            "goal_min_confidence": 0.8,
            "nominal_position_error_ratio": 0.1,
        },
    }


def test_param_versions_are_digest_addressed_and_database_immutable(tmp_path):
    database, versions = repository(tmp_path)

    stored = versions.create(
        snapshot=base_snapshot(),
        source="manual",
        version_id="param-v1",
        approval={"approved_by": "operator-a"},
    )

    assert len(stored.digest) == 64
    assert versions.get("param-v1").snapshot == base_snapshot()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with database.connection() as connection:
            connection.execute(
                "UPDATE param_versions SET source = 'changed' WHERE id = ?",
                ("param-v1",),
            )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with database.connection() as connection:
            connection.execute(
                "DELETE FROM param_versions WHERE id = ?",
                ("param-v1",),
            )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("safety.heartbeat_timeout_ms", 700),
        ("tof.front_stop_mm", 100),
        ("tof.danger_stop_mm", 50),
        ("motor.max_pwm", 200),
        ("arrival_verification.goal_min_confidence", 0.7),
        (
            "arrival_verification.nominal_position_error_ratio",
            0.2,
        ),
    ],
)
def test_automatic_versions_cannot_modify_safety_or_completion_domain(
    tmp_path,
    path,
    value,
):
    _database, versions = repository(tmp_path)
    parent = versions.create(
        snapshot=base_snapshot(),
        source="manual",
        version_id="param-v1",
    )
    child = base_snapshot()
    section, key = path.split(".", 1)
    child[section][key] = value

    with pytest.raises(ParamPolicyError, match=path):
        versions.create(
            snapshot=child,
            source="agent_auto",
            parent_id=parent.version_id,
        )


def test_automatic_version_may_change_motion_parameter_with_evidence(tmp_path):
    _database, versions = repository(tmp_path)
    parent = versions.create(
        snapshot=base_snapshot(),
        source="manual",
        version_id="param-v1",
    )
    child = base_snapshot()
    child["motor"]["base_speed"] = 0.26

    stored = versions.create(
        snapshot=child,
        source="agent_auto",
        parent_id=parent.version_id,
        evidence={"run_id": "run-fake-1", "reason": "undershoot"},
    )

    assert stored.parent_id == "param-v1"
    assert stored.diff == {"motor.base_speed": [0.25, 0.26]}
