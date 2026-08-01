import sqlite3
from pathlib import Path

import pytest

from rdk_maze_tuner.platform.config import PlatformConfig
from rdk_maze_tuner.platform.database import Database


REQUIRED_TABLES = {
    "audit_events",
    "users",
    "sessions",
    "control_lease",
    "devices",
    "maps",
    "map_versions",
    "param_versions",
    "runs",
    "events",
    "scores",
    "artifacts",
    "advisor_candidates",
    "physical_profiles",
}


def test_platform_config_uses_local_default_server_default_and_override(tmp_path):
    local = PlatformConfig.from_env({}, project_root=tmp_path)
    server = PlatformConfig.from_env({"MAZE_ENV": "server"}, project_root=tmp_path)
    overridden = PlatformConfig.from_env(
        {"MAZE_DATA_DIR": "custom/data"},
        project_root=tmp_path,
    )

    assert local.data_dir == tmp_path / ".local" / "maze-data"
    assert local.database_path == local.data_dir / "maze-platform.sqlite3"
    assert local.runs_dir == local.data_dir / "runs"
    assert server.data_dir == Path("/srv/maze/shared")
    assert overridden.data_dir == tmp_path / "custom" / "data"

    local.ensure_directories()
    assert local.runs_dir.is_dir()
    assert local.artifacts_dir.is_dir()


def test_database_initializes_required_tables_and_is_repeatable(tmp_path):
    database = Database(tmp_path / "platform.sqlite3")

    first_versions = database.initialize()
    second_versions = database.initialize()

    assert first_versions == (1, 2, 3)
    assert second_versions == ()

    with database.connection() as connection:
        table_names = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        migrations = connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()

    assert REQUIRED_TABLES <= table_names
    assert [(row["version"], row["name"]) for row in migrations] == [
        (1, "001_initial.sql"),
        (2, "002_auth_audit.sql"),
        (3, "003_physical_profiles.sql"),
    ]


def test_physical_run_identity_columns_are_immutable(tmp_path):
    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()

    with database.connection() as connection:
        connection.execute(
            """
            INSERT INTO physical_profiles (
                profile_id, digest, random_seed, snapshot_json,
                created_at_utc
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "normal-v1",
                "a" * 64,
                20260801,
                '{"profile_id":"normal-v1"}',
                "2026-08-01T00:00:00Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO runs (
                id, mode, status, created_at_utc,
                physical_profile_id, physical_profile_digest,
                physical_profile_snapshot_json, random_seed,
                controller_version, webots_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "run-physical",
                "simulation",
                "PREFLIGHT",
                "2026-08-01T00:00:00Z",
                "normal-v1",
                "a" * 64,
                '{"profile_id":"normal-v1"}',
                20260801,
                "0.2.0",
                "R2025a",
            ),
        )

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with database.connection() as connection:
            connection.execute(
                """
                UPDATE runs
                SET physical_profile_digest = ?
                WHERE id = ?
                """,
                ("b" * 64, "run-physical"),
            )


def test_database_connections_enable_foreign_keys_wal_and_busy_timeout(tmp_path):
    database = Database(tmp_path / "platform.sqlite3", busy_timeout_ms=7_500)
    database.initialize()

    with database.connection() as connection:
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

    assert foreign_keys == 1
    assert journal_mode.lower() == "wal"
    assert busy_timeout == 7_500


def test_database_enforces_foreign_keys(tmp_path):
    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()

    with pytest.raises(sqlite3.IntegrityError):
        with database.connection() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    id, user_id, token_digest, csrf_digest, expires_at_utc, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "session-1",
                    "missing-user",
                    "token-digest",
                    "csrf-digest",
                    "2026-08-02T00:00:00Z",
                    "2026-08-01T00:00:00Z",
                ),
            )
