import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rdk_maze_tuner.platform.database import Database
from rdk_maze_tuner.platform.scoring import (
    RawMetricsConflictError,
    ScoringService,
)


FIXED_UTC = datetime(2026, 8, 1, 14, 0, tzinfo=UTC)
PROFILE_PATH = "rdk_maze_tuner/config/score_profile_v1.yaml"


def create_run(database: Database, run_id: str = "run-score-1") -> None:
    with database.connection() as connection:
        connection.execute(
            """
            INSERT INTO runs (
                id, mode, status, created_at_utc,
                started_at_utc, ended_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                "simulation",
                "COMPLETED",
                "2026-08-01T13:59:00Z",
                "2026-08-01T14:00:00Z",
                "2026-08-01T14:00:10Z",
            ),
        )


def complete_metrics() -> dict:
    return {
        "completed": True,
        "goal_reached": True,
        "map_wall_accuracy": 1.0,
        "grid_position_accuracy": 1.0,
        "position_rmse_mm": 0.0,
        "heading_mae_deg": 0.0,
        "path_length_mm": 1000.0,
        "optimal_path_length_mm": 1000.0,
        "elapsed_s": 10.0,
        "target_time_s": 10.0,
        "stop_error_p95_mm": 0.0,
        "action_success_rate": 1.0,
        "retry_count": 0,
        "max_slip_rate": 0.0,
        "avg_slip_rate": 0.0,
        "collision_count": 0,
        "boundary_violation_count": 0,
        "timeout_count": 0,
        "estop_count": 0,
        "safety_event_count": 0,
    }


def make_service(tmp_path):
    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()
    create_run(database)
    service = ScoringService(
        database=database,
        runs_dir=tmp_path / "runs",
        profile_path=PROFILE_PATH,
        utc_now=lambda: FIXED_UTC,
        id_factory=iter(
            ("artifact-metrics-1", "score-v1", "score-v2")
        ).__next__,
    )
    return database, service


def test_raw_metrics_are_immutable_and_perfect_run_scores_100(tmp_path):
    database, service = make_service(tmp_path)
    metrics = complete_metrics()

    raw = service.record_raw_metrics("run-score-1", metrics)
    score = service.score_run("run-score-1")

    assert raw["metrics"] == metrics
    assert score["profile_version"] == "score-profile-v1"
    assert score["total_score"] == 100.0
    assert sum(
        category["score"]
        for category in score["breakdown"].values()
    ) == 100.0
    assert service.get_raw_metrics("run-score-1") == raw
    with database.connection() as connection:
        artifact = connection.execute(
            """
            SELECT kind, relative_path, sha256
            FROM artifacts
            WHERE run_id = ?
            """,
            ("run-score-1",),
        ).fetchone()
    assert artifact["kind"] == "raw_metrics"
    assert artifact["sha256"] == raw["digest"]
    assert (
        tmp_path / artifact["relative_path"]
    ).read_text(encoding="utf-8").endswith("\n")


def test_new_profile_rescores_without_changing_raw_metrics(tmp_path):
    _database, service = make_service(tmp_path)
    metrics = complete_metrics()
    metrics["map_wall_accuracy"] = 0.5
    raw = service.record_raw_metrics("run-score-1", metrics)
    original_bytes = (
        tmp_path / "runs" / "run-score-1" / "raw_metrics.json"
    ).read_bytes()
    first = service.score_run("run-score-1")
    custom_profile = json.loads(
        Path(PROFILE_PATH).read_text(encoding="utf-8")
    )
    custom_profile["profile_version"] = "score-profile-v2"
    custom_profile["weights"]["completion_goal"] = 15
    custom_profile["weights"]["map_accuracy"] = 40
    custom_profile["weights"]["path_time"] = 12
    custom_profile_path = tmp_path / "score-v2.yaml"
    custom_profile_path.write_text(
        json.dumps(custom_profile, ensure_ascii=False),
        encoding="utf-8",
    )

    second = service.score_run(
        "run-score-1",
        profile_path=custom_profile_path,
    )

    assert first["profile_version"] == "score-profile-v1"
    assert second["profile_version"] == "score-profile-v2"
    assert first["total_score"] != second["total_score"]
    assert first["raw_metrics_digest"] == raw["digest"]
    assert second["raw_metrics_digest"] == raw["digest"]
    assert (
        tmp_path / "runs" / "run-score-1" / "raw_metrics.json"
    ).read_bytes() == original_bytes
    assert len(service.list_scores("run-score-1")) == 2


def test_conflicting_raw_metrics_are_rejected(tmp_path):
    _database, service = make_service(tmp_path)
    service.record_raw_metrics("run-score-1", complete_metrics())
    changed = complete_metrics()
    changed["collision_count"] = 1

    with pytest.raises(RawMetricsConflictError):
        service.record_raw_metrics("run-score-1", changed)
