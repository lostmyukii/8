import json
from datetime import UTC, datetime

from rdk_maze_tuner.platform.database import Database
from rdk_maze_tuner.platform.retention import RetentionManager


NOW = datetime(2026, 8, 1, 16, 0, tzinfo=UTC)


def add_run(database: Database, run_id: str) -> None:
    with database.connection() as connection:
        connection.execute(
            """
            INSERT INTO runs (
                id, mode, status, created_at_utc, ended_at_utc
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_id,
                "simulation",
                "COMPLETED",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:10:00Z",
            ),
        )


def add_artifact(
    database: Database,
    *,
    artifact_id: str,
    run_id: str,
    kind: str,
    relative_path: str,
    retained_until: str | None,
    pinned: bool = False,
    metadata: dict | None = None,
) -> None:
    with database.connection() as connection:
        connection.execute(
            """
            INSERT INTO artifacts (
                id, run_id, kind, relative_path, metadata_json,
                retained_until_utc, pinned, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                run_id,
                kind,
                relative_path,
                json.dumps(metadata or {}),
                retained_until,
                int(pinned),
                "2026-01-01T00:00:00Z",
            ),
        )


def make_manager(tmp_path):
    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()
    add_run(database, "run-retention-1")
    manager = RetentionManager(
        database=database,
        data_dir=tmp_path,
        utc_now=lambda: NOW,
    )
    return database, manager


def test_expired_video_is_deleted_but_pinned_and_referenced_evidence_remain(
    tmp_path,
):
    database, manager = make_manager(tmp_path)
    expired = tmp_path / "runs/run-retention-1/video.mp4"
    pinned = tmp_path / "runs/run-retention-1/pinned.mp4"
    referenced = tmp_path / "runs/run-retention-1/report-video.mp4"
    for path in (expired, pinned, referenced):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
    due = "2026-07-01T00:00:00Z"
    add_artifact(
        database,
        artifact_id="artifact-expired",
        run_id="run-retention-1",
        kind="video",
        relative_path="runs/run-retention-1/video.mp4",
        retained_until=due,
    )
    add_artifact(
        database,
        artifact_id="artifact-pinned",
        run_id="run-retention-1",
        kind="video",
        relative_path="runs/run-retention-1/pinned.mp4",
        retained_until=due,
        pinned=True,
    )
    add_artifact(
        database,
        artifact_id="artifact-referenced",
        run_id="run-retention-1",
        kind="video",
        relative_path="runs/run-retention-1/report-video.mp4",
        retained_until=due,
        metadata={"references": ["competition-report"]},
    )

    result = manager.apply()

    assert result["deleted"] == ["artifact-expired"]
    assert not expired.exists()
    assert pinned.exists()
    assert referenced.exists()
    assert {
        item["artifact_id"]: item["reason"]
        for item in result["protected"]
    } == {
        "artifact-pinned": "pinned",
        "artifact-referenced": "referenced",
    }


def test_schedule_assigns_30_day_video_and_180_day_evidence_deadlines(
    tmp_path,
):
    database, manager = make_manager(tmp_path)
    for artifact_id, kind, name in (
        ("video", "video", "video.mp4"),
        ("metrics", "raw_metrics", "raw_metrics.json"),
        ("events", "events_jsonl", "events.jsonl"),
    ):
        add_artifact(
            database,
            artifact_id=artifact_id,
            run_id="run-retention-1",
            kind=kind,
            relative_path=f"runs/run-retention-1/{name}",
            retained_until=None,
        )

    scheduled = manager.schedule_run("run-retention-1")

    assert scheduled["video"].startswith("2026-01-31")
    assert scheduled["raw_metrics"].startswith("2026-06-30")
    assert scheduled["events_jsonl"].startswith("2026-06-30")


def test_unsafe_artifact_path_is_never_deleted(tmp_path):
    database, manager = make_manager(tmp_path)
    outside = tmp_path.parent / "outside-evidence.mp4"
    outside.write_bytes(b"keep")
    add_artifact(
        database,
        artifact_id="artifact-unsafe",
        run_id="run-retention-1",
        kind="video",
        relative_path="../outside-evidence.mp4",
        retained_until="2026-07-01T00:00:00Z",
    )

    result = manager.apply()

    assert outside.exists()
    assert result["errors"][0]["artifact_id"] == "artifact-unsafe"
    assert result["errors"][0]["reason"] == "unsafe_path"
