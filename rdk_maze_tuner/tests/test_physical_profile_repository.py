from dataclasses import replace
from datetime import UTC, datetime

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from rdk_maze_tuner.dashboard.app import create_app
from rdk_maze_tuner.platform.auth import AuthService
from rdk_maze_tuner.platform.database import Database
from rdk_maze_tuner.platform.physical_profile_repository import (
    PhysicalProfileConflictError,
    PhysicalProfileRepository,
)
from simulation.webots.maze_car.physical_config import (
    PhysicalProfileRepository as YamlPhysicalProfileRepository,
)


TEST_PASSWORD = "correct horse battery staple"


def make_repository(tmp_path):
    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()
    repository = PhysicalProfileRepository(
        database=database,
        utc_now=lambda: datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
    )
    return database, repository


def test_sync_imports_four_immutable_digest_bound_profiles_idempotently(
    tmp_path,
):
    database, repository = make_repository(tmp_path)

    first = repository.sync_from_yaml()
    second = repository.sync_from_yaml()
    profiles = repository.list_profiles()

    assert first == {"inserted": 4, "unchanged": 0}
    assert second == {"inserted": 0, "unchanged": 4}
    assert {item.profile_id for item in profiles} == {
        "normal-v1",
        "low-v1",
        "asymmetric-v1",
        "local-patch-v1",
    }
    normal = repository.get("normal-v1")
    assert normal.snapshot["profile_id"] == "normal-v1"
    assert normal.snapshot["random_seed"] == 20260801
    assert normal.digest == YamlPhysicalProfileRepository().get(
        "normal-v1"
    ).digest
    with database.connection() as connection:
        stored = connection.execute(
            "SELECT snapshot_json FROM physical_profiles WHERE profile_id = ?",
            ("normal-v1",),
        ).fetchone()
    assert '"profile_id":"normal-v1"' in stored["snapshot_json"]


def test_same_profile_id_with_different_content_cannot_overwrite_snapshot(
    tmp_path,
):
    _database, repository = make_repository(tmp_path)
    source = YamlPhysicalProfileRepository().get("normal-v1")
    repository.import_profile(source)

    with pytest.raises(PhysicalProfileConflictError, match="different"):
        repository.import_profile(
            replace(source, random_seed=source.random_seed + 1)
        )

    assert repository.get("normal-v1").random_seed == 20260801


def test_read_only_profile_api_requires_login_and_returns_snapshot(tmp_path):
    database, repository = make_repository(tmp_path)
    repository.sync_from_yaml()
    auth = AuthService(
        database=database,
        password_hasher=PasswordHasher(
            time_cost=1,
            memory_cost=8_192,
            parallelism=1,
        ),
    )
    auth.create_user("operator-a", TEST_PASSWORD)
    app = create_app(
        database=database,
        auth_service=auth,
        physical_profile_repository=repository,
    )

    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/api/physical-profiles").status_code == 401
        login = client.post(
            "/api/auth/login",
            json={
                "username": "operator-a",
                "password": TEST_PASSWORD,
            },
        )
        assert login.status_code == 200
        listed = client.get("/api/physical-profiles")
        detail = client.get("/api/physical-profiles/normal-v1")
        missing = client.get("/api/physical-profiles/missing-v1")

    assert listed.status_code == 200
    assert len(listed.json()["physical_profiles"]) == 4
    assert detail.status_code == 200
    assert detail.json()["profile_id"] == "normal-v1"
    assert detail.json()["snapshot"]["geometry"]["wheel_radius_m"] == 0.0325
    assert missing.status_code == 404
