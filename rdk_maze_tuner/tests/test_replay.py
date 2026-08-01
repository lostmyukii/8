import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rdk_maze_tuner.dashboard.app import create_app
from rdk_maze_tuner.platform.auth import AuthService
from rdk_maze_tuner.platform.database import Database
from rdk_maze_tuner.platform.event_store import EventStore
from rdk_maze_tuner.platform.replay import ReplayService, RunFinalizer
from rdk_maze_tuner.platform.retention import RetentionManager
from rdk_maze_tuner.platform.scoring import ScoringService
from rdk_maze_tuner.platform.video_recorder import (
    FfmpegVideoRecorder,
    VideoArtifactRegistry,
    VideoBandwidthError,
)
from simulation.webots.maze_car.physical_config import (
    PhysicalProfileRepository as YamlPhysicalProfileRepository,
)


FIXED_UTC = datetime(2026, 8, 1, 15, 0, tzinfo=UTC)
TEST_PASSWORD = "correct horse battery staple"


def create_run(database: Database, run_id: str = "run-replay-1") -> None:
    profile = YamlPhysicalProfileRepository().get("normal-v1")
    with database.connection() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO physical_profiles (
                profile_id, digest, random_seed, snapshot_json,
                created_at_utc
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "normal-v1",
                profile.digest,
                20260801,
                profile.canonical_json,
                "2026-08-01T14:58:00Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO runs (
                id, mode, status, created_at_utc,
                started_at_utc, ended_at_utc, metadata_json,
                physical_profile_id, physical_profile_digest,
                physical_profile_snapshot_json, random_seed,
                controller_version, webots_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                "simulation",
                "COMPLETED",
                "2026-08-01T14:59:00Z",
                "2026-08-01T15:00:00Z",
                "2026-08-01T15:00:02Z",
                json.dumps(
                    {
                        "map_version": "map-v1",
                        "param_version": "param-v1",
                    }
                ),
                "normal-v1",
                profile.digest,
                json.dumps(
                    {
                        "profile_id": "normal-v1",
                        "digest": profile.digest,
                        "random_seed": 20260801,
                        "snapshot": profile.to_dict(),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                20260801,
                "0.2.0",
                "R2025a",
            ),
        )


def make_replay(tmp_path):
    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()
    create_run(database)
    ticks = iter((1_000_000_000, 1_250_000_000, 2_000_000_000))
    event_ids = iter(("event-1", "event-2", "event-3"))
    store = EventStore(
        database=database,
        runs_dir=tmp_path / "runs",
        monotonic_ns=ticks.__next__,
        utc_now=lambda: FIXED_UTC,
        event_id_factory=event_ids.__next__,
    )
    store.append(
        run_id="run-replay-1",
        event_type="telemetry",
        source="simulation",
        payload={
            "x_mm": 150.0,
            "y_mm": 250.0,
            "yaw_deg": 0.0,
            "pose_confidence": 0.9,
            "front_mm": 300,
            "raw_front_mm": 301,
            "raw_left_mm": 210,
            "raw_right_mm": 220,
            "left_mm": 209,
            "right_mm": 219,
            "enc_left": 1350,
            "enc_right": 1348,
            "wheel_speed_left_rad_s": 2.0,
            "wheel_speed_right_rad_s": 1.9,
            "imu_available": True,
            "imu_yaw_deg": 0.5,
            "yaw_rate_dps": 0.2,
            "state": "MOVING_CELL",
            "slip_left": 0.04,
            "slip_right": 0.03,
            "friction_profile": "normal",
            "sim_truth": {
                "x_mm": 152.0,
                "y_mm": 248.0,
                "yaw_deg": 1.0,
                "left_slip_rate": 0.05,
                "right_slip_rate": 0.04,
                "active_surface": "normal",
                "collision_count": 0,
            },
        },
    )
    store.append(
        run_id="run-replay-1",
        event_type="done",
        source="esp32",
        payload={
            "action_id": "a-1",
            "name": "move_cell",
            "success": True,
        },
    )
    store.append(
        run_id="run-replay-1",
        event_type="task.completed",
        source="task_orchestrator",
        payload={"reason": "goal_reached"},
    )
    service = ReplayService(
        database=database,
        event_store=store,
        data_dir=tmp_path,
        utc_now=lambda: FIXED_UTC,
        id_factory=iter(
            ("artifact-events-1", "artifact-replay-1")
        ).__next__,
    )
    return database, store, service


def test_replay_manifest_uses_relative_monotonic_time_and_separate_truth(
    tmp_path,
):
    _database, _store, service = make_replay(tmp_path)

    manifest = service.build_manifest("run-replay-1")

    assert [item["t_ms"] for item in manifest["timeline"]] == [
        0.0,
        250.0,
        1000.0,
    ]
    assert manifest["duration_ms"] == 1000.0
    assert manifest["timeline"][1]["key_event"] is True
    assert manifest["tracks"]["trajectory"][0]["x_mm"] == 150.0
    assert manifest["tracks"]["truth"][0]["x_mm"] == 152.0
    assert manifest["tracks"]["trajectory"][0]["source"] == "estimate"
    assert manifest["tracks"]["truth"][0]["source"] == "simulation_truth"
    assert manifest["map_version"] == "map-v1"
    assert manifest["param_version"] == "param-v1"
    assert manifest["media"]["complete"] is False
    assert manifest["physical_profile"]["profile_id"] == "normal-v1"
    assert set(manifest["tracks"]) >= {
        "trajectory",
        "truth",
        "physical_profile",
        "wheel",
        "tof",
        "imu",
        "control",
        "slip_estimate",
        "sim_truth",
        "surface",
        "fault",
    }
    assert len(manifest["tracks"]["wheel"]) == 1
    assert len(manifest["tracks"]["sim_truth"]) == 1


def test_finalize_writes_replay_manifest_and_indexes_jsonl(tmp_path):
    database, _store, service = make_replay(tmp_path)

    result = service.finalize_run("run-replay-1")

    assert result["status"] == "media_incomplete"
    manifest_path = (
        tmp_path / "runs" / "run-replay-1" / "replay.json"
    )
    assert manifest_path.is_file()
    assert json.loads(
        manifest_path.read_text(encoding="utf-8")
    )["run_id"] == "run-replay-1"
    with database.connection() as connection:
        kinds = {
            row["kind"]
            for row in connection.execute(
                "SELECT kind FROM artifacts WHERE run_id = ?",
                ("run-replay-1",),
            )
        }
    assert {"events_jsonl", "replay_manifest"} <= kinds


def test_run_finalizer_freezes_metrics_scores_and_replay(tmp_path):
    database, store, replay = make_replay(tmp_path)
    scoring = ScoringService(
        database=database,
        runs_dir=tmp_path / "runs",
        utc_now=lambda: FIXED_UTC,
        id_factory=iter(
            ("artifact-metrics-1", "score-final-1")
        ).__next__,
    )
    retention = RetentionManager(
        database=database,
        data_dir=tmp_path,
        utc_now=lambda: FIXED_UTC,
    )
    finalizer = RunFinalizer(
        scoring=scoring,
        replay=replay,
        retention=retention,
        event_store=store,
    )

    result = finalizer("run-replay-1")

    assert result["status"] == "media_incomplete"
    assert result["score"]["profile_version"] == "score-profile-v1"
    assert result["score"]["total_score"] > 0
    assert (
        tmp_path / "runs" / "run-replay-1" / "raw_metrics.json"
    ).is_file()
    assert (
        tmp_path / "runs" / "run-replay-1" / "replay.json"
    ).is_file()
    with database.connection() as connection:
        kinds = {
            row["kind"]
            for row in connection.execute(
                "SELECT kind FROM artifacts WHERE run_id = ?",
                ("run-replay-1",),
            )
        }
    assert {
        "events_jsonl",
        "raw_metrics",
        "replay_manifest",
    } <= kinds


def test_run_replay_api_requires_login_and_exposes_structured_fallback(
    tmp_path,
):
    database, _store, replay = make_replay(tmp_path)
    replay.finalize_run("run-replay-1")
    auth = AuthService(database=database)
    auth.create_user("operator-a", TEST_PASSWORD)
    app = create_app(
        database=database,
        auth_service=auth,
        replay_service=replay,
    )

    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/api/runs/run-replay-1").status_code == 401
        login = client.post(
            "/api/auth/login",
            json={
                "username": "operator-a",
                "password": TEST_PASSWORD,
            },
        )
        assert login.status_code == 200

        run = client.get("/api/runs/run-replay-1")
        runs = client.get("/api/runs")
        events = client.get("/api/runs/run-replay-1/events")
        manifest = client.get("/api/runs/run-replay-1/replay")
        video = client.get("/api/runs/run-replay-1/video")

    assert run.status_code == 200
    assert runs.status_code == 200
    assert runs.json()["runs"][0]["run_id"] == "run-replay-1"
    assert run.json()["media_complete"] is False
    assert len(events.json()["events"]) == 3
    assert manifest.json()["timeline"][0]["t_ms"] == 0.0
    assert video.status_code == 404


class CaptureStream(io.BytesIO):
    def close(self):
        self.flush()


class FakeProcess:
    def __init__(self, output_path: Path) -> None:
        self.output_path = output_path
        self.stdin = CaptureStream()
        self.returncode = None

    def wait(self, timeout=None):
        self.output_path.write_bytes(b"fake-mp4")
        self.returncode = 0
        return 0

    def terminate(self):
        self.returncode = -15


def test_real_video_recorder_limits_bandwidth_and_reports_completion(tmp_path):
    output_path = tmp_path / "run.mp4"
    processes = []

    def process_factory(command, **_kwargs):
        process = FakeProcess(Path(command[-1]))
        processes.append((command, process))
        return process

    recorder = FfmpegVideoRecorder(
        output_path=output_path,
        fps=5,
        max_bits_per_second=3_000_000,
        process_factory=process_factory,
    )
    recorder.start_real()
    recorder.write_jpeg(b"\xff\xd8small-jpeg\xff\xd9", timestamp_ns=0)
    result = recorder.stop()

    assert result["complete"] is True
    assert result["path"] == str(output_path)
    assert "image2pipe" in processes[0][0]
    assert processes[0][1].stdin.getvalue().startswith(b"\xff\xd8")

    limited = FfmpegVideoRecorder(
        output_path=tmp_path / "limited.mp4",
        fps=5,
        max_bits_per_second=3_000_000,
        process_factory=process_factory,
    )
    limited.start_real()
    oversized = b"\xff\xd8" + b"x" * 400_000 + b"\xff\xd9"
    with pytest.raises(VideoBandwidthError):
        limited.write_jpeg(oversized, timestamp_ns=0)


def test_simulation_capture_command_uses_xvfb_display_and_fixed_geometry(
    tmp_path,
):
    command = FfmpegVideoRecorder.simulation_command(
        output_path=tmp_path / "sim.mp4",
        display=":99",
        geometry="640x360",
        fps=10,
    )

    assert "-f" in command
    assert "x11grab" in command
    assert ":99" in command
    assert "640x360" in command
    assert command[-1].endswith("sim.mp4")


def test_complete_video_artifact_is_hashed_and_exposed_to_replay(tmp_path):
    database, _store, replay = make_replay(tmp_path)
    replay.finalize_run("run-replay-1")
    video_path = tmp_path / "runs" / "run-replay-1" / "video.mp4"
    video_path.write_bytes(b"verified-video")
    registry = VideoArtifactRegistry(
        database=database,
        data_dir=tmp_path,
        utc_now=lambda: FIXED_UTC,
        id_factory=lambda: "artifact-video-1",
    )

    artifact = registry.register(
        run_id="run-replay-1",
        result={
            "complete": True,
            "mode": "simulation",
            "path": str(video_path),
            "frame_count": 50,
            "reason": None,
            "started_monotonic_ns": 900_000_000,
            "ended_monotonic_ns": 2_100_000_000,
        },
    )
    run = replay.get_run("run-replay-1")

    assert artifact["sha256"]
    assert len(artifact["sha256"]) == 64
    assert run["media_complete"] is True
    assert run["video"]["sha256"] == artifact["sha256"]
    assert replay.video_path("run-replay-1") == video_path
    media = replay.get_manifest("run-replay-1")["media"]
    assert media["complete"] is True
    assert media["timeline_offset_ms"] == -100.0
