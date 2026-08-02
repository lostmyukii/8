"""Monotonic-time run summaries and synchronized replay manifests."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from rdk_maze_tuner.core.logger import write_json_atomic

from .database import Database
from .event_store import EventStore, SAFE_RUN_ID
from .retention import RetentionManager
from .scoring import ScoringService


KEY_EVENT_TYPES = {
    "ack",
    "done",
    "error",
    "task.started",
    "task.paused",
    "task.completed",
    "task.error",
    "task.estop",
    "step.goal_verified",
    "param_change",
    "approval.created",
    "approval.decided",
}


class RunNotFoundError(LookupError):
    """Raised when replay data is requested for an unknown run."""


class RunFinalizer:
    """Freeze score evidence and build the synchronized run archive."""

    def __init__(
        self,
        *,
        scoring: ScoringService,
        replay: "ReplayService",
        retention: RetentionManager,
        event_store: EventStore,
    ) -> None:
        self.scoring = scoring
        self.replay = replay
        self.retention = retention
        self.event_store = event_store

    def __call__(self, run_id: str) -> dict[str, Any]:
        metrics = self.scoring.derive_raw_metrics(
            run_id,
            self.event_store,
        )
        raw = self.scoring.record_raw_metrics(run_id, metrics)
        score = self.scoring.score_run(run_id)
        replay = self.replay.finalize_run(run_id)
        retention = self.retention.schedule_run(run_id)
        return {
            "run_id": run_id,
            "status": replay["status"],
            "media_complete": replay["media_complete"],
            "raw_metrics_digest": raw["digest"],
            "score": score,
            "replay": replay,
            "retention": retention,
        }


class ReplayService:
    def __init__(
        self,
        *,
        database: Database,
        event_store: EventStore,
        data_dir: Path,
        utc_now: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.database = database
        self.event_store = event_store
        self.data_dir = Path(data_dir)
        self.runs_dir = self.data_dir / "runs"
        self.utc_now = utc_now or (lambda: datetime.now(UTC))
        self.id_factory = id_factory or (lambda: str(uuid.uuid4()))

    def list_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("limit must be an integer")
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT id
                FROM runs
                ORDER BY
                    COALESCE(ended_at_utc, started_at_utc, created_at_utc) DESC,
                    id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self.get_run(row["id"]) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self._run_row(run_id)
        scores = self._scores(run_id)
        artifacts = self._artifacts(run_id)
        video = self._video_descriptor(run_id, artifacts)
        return {
            **run,
            "scores": scores,
            "latest_score": scores[-1] if scores else None,
            "artifacts": artifacts,
            "media_complete": video["complete"],
            "video": video,
        }

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        self._run_row(run_id)
        return self.event_store.list_events(run_id)

    def build_manifest(self, run_id: str) -> dict[str, Any]:
        run = self._run_row(run_id)
        events = self.event_store.list_events(run_id)
        base_ns = events[0]["monotonic_ns"] if events else 0
        timeline = []
        trajectory = []
        truth = []
        evidence_tracks: dict[str, list[dict[str, Any]]] = {
            "physical_profile": [],
            "wheel": [],
            "tof": [],
            "imu": [],
            "control": [],
            "slip_estimate": [],
            "sim_truth": [],
            "surface": [],
            "fault": [],
            "route": [],
            "action": [],
            "fused_pose": [],
            "recovery": [],
            "parameter_snapshot": [],
            "map_identity": [],
        }
        physical_profile = _run_physical_profile(run)
        if physical_profile is not None:
            evidence_tracks["physical_profile"].append(
                {
                    "t_ms": 0.0,
                    "source": "run_snapshot",
                    "payload": physical_profile,
                }
            )
        for event in events:
            t_ms = round(
                (event["monotonic_ns"] - base_ns) / 1_000_000.0,
                3,
            )
            payload = event.get("payload")
            if (
                event["type"] == "step.goal_reached"
                and isinstance(payload, Mapping)
            ):
                payload = {
                    **payload,
                    "legacy_logical_only": True,
                    "verification_label": "旧版逻辑到达",
                }
            item = {
                "event_id": event["event_id"],
                "t_ms": t_ms,
                "type": event["type"],
                "source": event["source"],
                "channel": _channel(event["type"]),
                "key_event": _is_key_event(event["type"]),
                "payload": payload,
            }
            timeline.append(item)
            if isinstance(payload, Mapping):
                for channel, evidence in _evidence_payloads(
                    event_type=event["type"],
                    payload=payload,
                ).items():
                    evidence_tracks[channel].append(
                        {
                            "t_ms": t_ms,
                            "source": event["source"],
                            "payload": evidence,
                        }
                    )
                estimated = _pose_point(payload, t_ms=t_ms)
                if estimated is not None:
                    trajectory.append(
                        {**estimated, "source": "estimate"}
                    )
                truth_payload = payload.get("sim_truth")
                if isinstance(truth_payload, Mapping):
                    truth_point = _pose_point(
                        truth_payload,
                        t_ms=t_ms,
                    )
                    if truth_point is not None:
                        truth.append(
                            {
                                **truth_point,
                                "source": "simulation_truth",
                            }
                        )

        metadata = run["metadata"]
        artifacts = self._artifacts(run_id)
        video = self._synchronized_video(
            self._video_descriptor(run_id, artifacts),
            base_ns,
        )
        scores = self._scores(run_id)
        return {
            "schema_version": 2,
            "run_id": run_id,
            "mode": run["mode"],
            "status": run["status"],
            "created_at_utc": run["created_at_utc"],
            "started_at_utc": run["started_at_utc"],
            "ended_at_utc": run["ended_at_utc"],
            "generated_at_utc": _utc_text(self.utc_now()),
            "time_origin_monotonic_ns": base_ns,
            "duration_ms": (
                timeline[-1]["t_ms"] if timeline else 0.0
            ),
            "map_version": (
                run.get("map_version_id")
                or metadata.get("map_version")
            ),
            "param_version": (
                run.get("param_version_id")
                or metadata.get("param_version")
            ),
            "physical_profile": physical_profile,
            "timeline": timeline,
            "key_events": [
                item for item in timeline if item["key_event"]
            ],
            "tracks": {
                "trajectory": trajectory,
                "truth": truth,
                **evidence_tracks,
            },
            "media": video,
            "score": scores[-1] if scores else None,
            "artifacts": artifacts,
        }

    def finalize_run(self, run_id: str) -> dict[str, Any]:
        self._run_row(run_id)
        events_path = self.runs_dir / run_id / "events.jsonl"
        if events_path.is_file():
            self._register_artifact(
                run_id=run_id,
                kind="events_jsonl",
                relative_path=f"runs/{run_id}/events.jsonl",
                sha256=_file_digest(events_path),
                metadata={
                    "schema_version": 1,
                    "status": "complete",
                    "append_only": True,
                },
                retention_days=180,
            )
        manifest = self.build_manifest(run_id)
        manifest_path = self.runs_dir / run_id / "replay.json"
        write_json_atomic(manifest_path, manifest)
        self._register_artifact(
            run_id=run_id,
            kind="replay_manifest",
            relative_path=f"runs/{run_id}/replay.json",
            sha256=_file_digest(manifest_path),
            metadata={
                "schema_version": 1,
                "status": "complete",
                "media_complete": manifest["media"]["complete"],
            },
            retention_days=180,
        )
        return {
            "run_id": run_id,
            "status": (
                "complete"
                if manifest["media"]["complete"]
                else "media_incomplete"
            ),
            "media_complete": manifest["media"]["complete"],
            "manifest_path": str(manifest_path),
        }

    def get_manifest(self, run_id: str) -> dict[str, Any]:
        self._run_row(run_id)
        path = self.runs_dir / run_id / "replay.json"
        if not path.is_file():
            return self.build_manifest(run_id)
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("run_id") != run_id:
            raise ValueError(f"invalid replay manifest for {run_id}")
        if int(value.get("schema_version") or 0) < 2:
            value = self.build_manifest(run_id)
        artifacts = self._artifacts(run_id)
        scores = self._scores(run_id)
        value["media"] = self._synchronized_video(
            self._video_descriptor(run_id, artifacts),
            value.get("time_origin_monotonic_ns"),
        )
        value["score"] = scores[-1] if scores else None
        value["artifacts"] = artifacts
        return value

    def video_path(self, run_id: str) -> Path | None:
        artifacts = self._artifacts(run_id)
        descriptor = self._video_descriptor(run_id, artifacts)
        if not descriptor["complete"] or not descriptor.get("relative_path"):
            return None
        return self._safe_path(descriptor["relative_path"])

    def _run_row(self, run_id: str) -> dict[str, Any]:
        _validate_run_id(run_id)
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT id, mode, status, map_version_id,
                       param_version_id, device_id,
                       physical_profile_id,
                       physical_profile_digest,
                       physical_profile_snapshot_json,
                       random_seed, controller_version,
                       webots_version,
                       created_by_user_id, created_at_utc,
                       started_at_utc, ended_at_utc, metadata_json
                FROM runs
                WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            raise RunNotFoundError(f"run not found: {run_id}")
        result = dict(row)
        result["run_id"] = result.pop("id")
        physical_snapshot = result.pop(
            "physical_profile_snapshot_json"
        )
        result["physical_profile_snapshot"] = (
            None
            if physical_snapshot is None
            else _json_object(physical_snapshot)
        )
        result["metadata"] = _json_object(result.pop("metadata_json"))
        return result

    def _scores(self, run_id: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, profile_version, breakdown_json,
                       total_score, created_at_utc
                FROM scores
                WHERE run_id = ?
                ORDER BY created_at_utc, id
                """,
                (run_id,),
            ).fetchall()
        result = []
        for row in rows:
            breakdown = _json_object(row["breakdown_json"])
            result.append(
                {
                    "score_id": row["id"],
                    "profile_version": row["profile_version"],
                    "breakdown": breakdown.get(
                        "categories",
                        breakdown,
                    ),
                    "raw_metrics_digest": breakdown.get(
                        "raw_metrics_digest"
                    ),
                    "total_score": row["total_score"],
                    "created_at_utc": row["created_at_utc"],
                }
            )
        return result

    def _artifacts(self, run_id: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, kind, relative_path, sha256,
                       metadata_json, retained_until_utc,
                       pinned, created_at_utc
                FROM artifacts
                WHERE run_id = ?
                ORDER BY created_at_utc, id
                """,
                (run_id,),
            ).fetchall()
        return [
            {
                "artifact_id": row["id"],
                "kind": row["kind"],
                "relative_path": row["relative_path"],
                "sha256": row["sha256"],
                "metadata": _json_object(row["metadata_json"]),
                "retained_until_utc": row["retained_until_utc"],
                "pinned": bool(row["pinned"]),
                "created_at_utc": row["created_at_utc"],
            }
            for row in rows
        ]

    def _video_descriptor(
        self,
        run_id: str,
        artifacts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        videos = [
            artifact
            for artifact in artifacts
            if artifact["kind"] == "video"
            and artifact["metadata"].get("status") != "deleted"
        ]
        if not videos:
            return {
                "complete": False,
                "status": "missing",
                "reason": "video artifact not available",
                "url": None,
                "relative_path": None,
            }
        video = videos[-1]
        try:
            path = self._safe_path(video["relative_path"])
        except ValueError:
            return {
                "complete": False,
                "status": "unsafe_path",
                "reason": "video artifact path is unsafe",
                "url": None,
                "relative_path": video["relative_path"],
            }
        complete = (
            path.is_file()
            and path.stat().st_size > 0
            and video["metadata"].get("status", "complete") == "complete"
        )
        return {
            "complete": complete,
            "status": "complete" if complete else "incomplete",
            "reason": None if complete else "video file is incomplete",
            "url": f"/api/runs/{run_id}/video" if complete else None,
            "relative_path": video["relative_path"],
            "sha256": video["sha256"],
            "started_monotonic_ns": video["metadata"].get(
                "started_monotonic_ns"
            ),
            "ended_monotonic_ns": video["metadata"].get(
                "ended_monotonic_ns"
            ),
        }

    @staticmethod
    def _synchronized_video(
        descriptor: dict[str, Any],
        base_ns: Any,
    ) -> dict[str, Any]:
        result = dict(descriptor)
        started_ns = result.get("started_monotonic_ns")
        if _finite_number(started_ns) and _finite_number(base_ns):
            result["timeline_offset_ms"] = round(
                (float(started_ns) - float(base_ns)) / 1_000_000.0,
                3,
            )
        else:
            result["timeline_offset_ms"] = 0.0
        return result

    def _register_artifact(
        self,
        *,
        run_id: str,
        kind: str,
        relative_path: str,
        sha256: str,
        metadata: Mapping[str, Any],
        retention_days: int,
    ) -> None:
        now = self.utc_now()
        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO artifacts (
                    id, run_id, kind, relative_path, sha256,
                    metadata_json, retained_until_utc, pinned,
                    created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(relative_path) DO UPDATE SET
                    sha256 = excluded.sha256,
                    metadata_json = excluded.metadata_json,
                    retained_until_utc = excluded.retained_until_utc
                """,
                (
                    str(self.id_factory()),
                    run_id,
                    kind,
                    relative_path,
                    sha256,
                    _canonical_json(metadata),
                    _utc_text(now + timedelta(days=retention_days)),
                    _utc_text(now),
                ),
            )

    def _safe_path(self, relative_path: str) -> Path:
        root = self.data_dir.resolve()
        candidate = (self.data_dir / relative_path).resolve()
        if not candidate.is_relative_to(root):
            raise ValueError("artifact path escapes data directory")
        return candidate


def _pose_point(
    payload: Mapping[str, Any],
    *,
    t_ms: float,
) -> dict[str, Any] | None:
    fields = ("x_mm", "y_mm", "yaw_deg")
    if not all(_finite_number(payload.get(field)) for field in fields):
        return None
    point = {
        "t_ms": t_ms,
        "x_mm": float(payload["x_mm"]),
        "y_mm": float(payload["y_mm"]),
        "yaw_deg": float(payload["yaw_deg"]) % 360.0,
    }
    confidence = payload.get(
        "pose_confidence",
        payload.get("confidence"),
    )
    if _finite_number(confidence):
        point["confidence"] = float(confidence)
    return point


def _run_physical_profile(
    run: Mapping[str, Any],
) -> dict[str, Any] | None:
    profile_id = run.get("physical_profile_id")
    if not profile_id:
        return None
    return {
        "profile_id": profile_id,
        "digest": run.get("physical_profile_digest"),
        "random_seed": run.get("random_seed"),
        "controller_version": run.get("controller_version"),
        "webots_version": run.get("webots_version"),
        "snapshot": run.get("physical_profile_snapshot"),
    }


def _evidence_payloads(
    *,
    event_type: str,
    payload: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    wheel = _present_fields(
        payload,
        (
            "wheel_angle_left_rad",
            "wheel_angle_right_rad",
            "wheel_speed_left_rad_s",
            "wheel_speed_right_rad_s",
            "pwm_left",
            "pwm_right",
            "motor_torque_left_nm",
            "motor_torque_right_nm",
            "enc_left",
            "enc_right",
        ),
    )
    tof = _present_fields(
        payload,
        (
            "raw_front_mm",
            "raw_left_mm",
            "raw_right_mm",
            "front_mm",
            "left_mm",
            "right_mm",
            "quality_flags",
        ),
    )
    imu = _present_fields(
        payload,
        (
            "imu_available",
            "imu_yaw_deg",
            "yaw_rate_dps",
            "accel_forward_mps2",
            "pose_confidence",
        ),
    )
    control = _present_fields(
        payload,
        (
            "state",
            "action_id",
            "progress_ticks",
            "remaining_ticks",
            "heading_error_deg",
            "controller_period_ms",
            "motor_available_torque_nm",
        ),
    )
    slip = _present_fields(
        payload,
        (
            "slip_left",
            "slip_right",
            "slip_rate",
            "slip_quality",
            "equivalent_friction",
        ),
    )
    for name, value in (
        ("wheel", wheel),
        ("tof", tof),
        ("imu", imu),
        ("control", control),
        ("slip_estimate", slip),
    ):
        if value:
            result[name] = value
    sim_truth = payload.get("sim_truth")
    if isinstance(sim_truth, Mapping):
        result["sim_truth"] = dict(sim_truth)
    surface = _present_fields(
        payload,
        ("friction_profile", "active_surface"),
    )
    if isinstance(sim_truth, Mapping) and "active_surface" in sim_truth:
        surface["truth_active_surface"] = sim_truth["active_surface"]
    if surface:
        result["surface"] = surface
    if event_type == "error" or "safety" in event_type:
        fault = _present_fields(
            payload,
            (
                "code",
                "message",
                "action_id",
                "front_mm",
                "state",
            ),
        )
        result["fault"] = fault or {"event_type": event_type}
    if event_type == "route.planned":
        result["route"] = dict(payload)
    if (
        event_type in {"planned_action", "done", "error"}
        or event_type.startswith("motion.recovery.")
    ):
        result["action"] = dict(payload)
    if event_type in {"pose.updated", "pose.committed"}:
        result["fused_pose"] = dict(payload)
    if event_type.startswith("motion.recovery."):
        result["recovery"] = dict(payload)
    if event_type in {"param_snapshot", "param_change"}:
        result["parameter_snapshot"] = dict(payload)
    map_identity = _present_fields(
        payload,
        (
            "map_version_id",
            "map_digest",
            "source_map_version",
            "source_map_digest",
        ),
    )
    verification_goal = payload.get("goal")
    if isinstance(verification_goal, Mapping):
        map_identity.update(
            _present_fields(
                verification_goal,
                (
                    "source_map_version",
                    "source_map_digest",
                ),
            )
        )
    if map_identity:
        result["map_identity"] = map_identity
    return result


def _present_fields(
    payload: Mapping[str, Any],
    fields: tuple[str, ...],
) -> dict[str, Any]:
    return {
        field: payload[field]
        for field in fields
        if field in payload and payload[field] is not None
    }


def _channel(event_type: str) -> str:
    if event_type == "telemetry":
        return "telemetry"
    if event_type in {"ack", "done", "error"} or "action" in event_type:
        return "actions"
    if "param" in event_type or "approval" in event_type:
        return "parameters"
    if "estop" in event_type or "safety" in event_type:
        return "safety"
    if "control" in event_type or "lease" in event_type:
        return "control"
    return "task"


def _is_key_event(event_type: str) -> bool:
    return (
        event_type in KEY_EVENT_TYPES
        or event_type.endswith(".error")
        or event_type.endswith(".completed")
    )


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _validate_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or SAFE_RUN_ID.fullmatch(run_id) is None:
        raise ValueError("run_id contains unsafe characters")


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC clock must return a timezone-aware datetime")
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
