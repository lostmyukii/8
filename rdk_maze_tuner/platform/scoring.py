"""Immutable raw run metrics and versioned score-profile evaluation."""

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


DEFAULT_PROFILE_PATH = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "score_profile_v1.yaml"
)


class RawMetricsConflictError(RuntimeError):
    """Raised when immutable run metrics would be overwritten."""


class ScoreProfileError(ValueError):
    """Raised when a score profile is incomplete or inconsistent."""


class RunMetricsNotFoundError(LookupError):
    """Raised when a run or its frozen metrics cannot be found."""


class ScoringService:
    def __init__(
        self,
        *,
        database: Database,
        runs_dir: Path,
        profile_path: Path | str = DEFAULT_PROFILE_PATH,
        utc_now: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.database = database
        self.runs_dir = Path(runs_dir)
        self.profile_path = Path(profile_path)
        self.utc_now = utc_now or (lambda: datetime.now(UTC))
        self.id_factory = id_factory or (lambda: str(uuid.uuid4()))

    def record_raw_metrics(
        self,
        run_id: str,
        metrics: Mapping[str, Any],
    ) -> dict[str, Any]:
        _validate_run_id(run_id)
        if not isinstance(metrics, Mapping):
            raise ValueError("metrics must be an object")
        self._require_run(run_id)
        normalized = _json_object(metrics)
        digest = _digest(normalized)
        path = self.runs_dir / run_id / "raw_metrics.json"
        if path.exists():
            existing = _load_object(path)
            if existing.get("digest") != digest:
                raise RawMetricsConflictError(
                    f"raw metrics already frozen for {run_id}"
                )
            if existing.get("metrics") != normalized:
                raise RawMetricsConflictError(
                    f"raw metrics digest conflict for {run_id}"
                )
            return existing

        document = {
            "schema_version": 1,
            "run_id": run_id,
            "captured_at_utc": _utc_text(self.utc_now()),
            "digest": digest,
            "metrics": normalized,
        }
        write_json_atomic(path, document)
        self._register_artifact(
            run_id=run_id,
            kind="raw_metrics",
            relative_path=f"runs/{run_id}/raw_metrics.json",
            sha256=digest,
            retained_until=self.utc_now() + timedelta(days=180),
            metadata={
                "schema_version": 1,
                "status": "complete",
                "content_type": "application/json",
            },
        )
        return document

    def get_raw_metrics(self, run_id: str) -> dict[str, Any]:
        _validate_run_id(run_id)
        path = self.runs_dir / run_id / "raw_metrics.json"
        if not path.is_file():
            raise RunMetricsNotFoundError(
                f"raw metrics not found for {run_id}"
            )
        document = _load_object(path)
        metrics = document.get("metrics")
        if not isinstance(metrics, dict):
            raise RawMetricsConflictError(
                f"raw metrics document is invalid for {run_id}"
            )
        if document.get("digest") != _digest(metrics):
            raise RawMetricsConflictError(
                f"raw metrics digest mismatch for {run_id}"
            )
        return document

    def score_run(
        self,
        run_id: str,
        *,
        profile_path: Path | str | None = None,
    ) -> dict[str, Any]:
        raw = self.get_raw_metrics(run_id)
        profile = _load_profile(
            Path(profile_path) if profile_path is not None else self.profile_path
        )
        breakdown = _calculate_breakdown(raw["metrics"], profile)
        total = round(
            sum(item["score"] for item in breakdown.values()),
            4,
        )
        profile_version = profile["profile_version"]
        with self.database.connection() as connection:
            existing = connection.execute(
                """
                SELECT id, raw_metrics_json, breakdown_json,
                       total_score, created_at_utc
                FROM scores
                WHERE run_id = ? AND profile_version = ?
                """,
                (run_id, profile_version),
            ).fetchone()
            if existing is not None:
                return _score_row(
                    existing,
                    run_id=run_id,
                    profile_version=profile_version,
                )
            score_id = str(self.id_factory())
            connection.execute(
                """
                INSERT INTO scores (
                    id, run_id, profile_version, raw_metrics_json,
                    breakdown_json, total_score, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    score_id,
                    run_id,
                    profile_version,
                    _canonical_json(raw["metrics"]),
                    _canonical_json(
                        {
                            "raw_metrics_digest": raw["digest"],
                            "categories": breakdown,
                        }
                    ),
                    total,
                    _utc_text(self.utc_now()),
                ),
            )
        return {
            "score_id": score_id,
            "run_id": run_id,
            "profile_version": profile_version,
            "raw_metrics_digest": raw["digest"],
            "breakdown": breakdown,
            "total_score": total,
            "created_at_utc": _utc_text(self.utc_now()),
        }

    def list_scores(self, run_id: str) -> list[dict[str, Any]]:
        _validate_run_id(run_id)
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, profile_version, raw_metrics_json,
                       breakdown_json, total_score, created_at_utc
                FROM scores
                WHERE run_id = ?
                ORDER BY created_at_utc, id
                """,
                (run_id,),
            ).fetchall()
        return [
            _score_row(
                row,
                run_id=run_id,
                profile_version=row["profile_version"],
            )
            for row in rows
        ]

    def derive_raw_metrics(
        self,
        run_id: str,
        event_store: EventStore,
    ) -> dict[str, Any]:
        run = self._require_run(run_id)
        events = event_store.list_events(run_id)
        payloads = [
            event["payload"]
            for event in events
            if isinstance(event.get("payload"), Mapping)
        ]
        telemetry = [
            event["payload"]
            for event in events
            if event["type"] == "telemetry"
            and isinstance(event.get("payload"), Mapping)
        ]
        done_events = [
            event
            for event in events
            if event["type"] == "done"
            and isinstance(event.get("payload"), Mapping)
        ]
        action_errors = [
            event
            for event in events
            if event["type"] == "error"
            and isinstance(event.get("payload"), Mapping)
            and event["payload"].get("action_id")
        ]
        completion = next(
            (
                event
                for event in reversed(events)
                if event["type"] == "task.completed"
            ),
            None,
        )
        goal_verification = next(
            (
                event
                for event in reversed(events)
                if event["type"] == "step.goal_verified"
                and isinstance(event.get("payload"), Mapping)
                and (
                    (
                        event["payload"].get("verification")
                        or {}
                    ).get("verified")
                    is True
                )
            ),
            None,
        )
        attempts = len(done_events) + len(action_errors)
        position_errors = _numbers(
            payloads,
            "position_error_mm",
            scale=1.0,
        ) + _numbers(payloads, "truth_error_cm", scale=10.0)
        heading_errors = _numbers(payloads, "truth_yaw_error_deg")
        estimated_left_slip = _numbers(
            telemetry,
            "slip_left",
            absolute=True,
        )
        estimated_right_slip = _numbers(
            telemetry,
            "slip_right",
            absolute=True,
        )
        truth_left_slip = _nested_numbers(
            telemetry,
            "sim_truth",
            "left_slip_rate",
            absolute=True,
        )
        truth_right_slip = _nested_numbers(
            telemetry,
            "sim_truth",
            "right_slip_rate",
            absolute=True,
        )
        slip_values = _numbers(
            telemetry,
            "slip_rate",
            absolute=True,
        )
        if not slip_values:
            slip_values = (
                estimated_left_slip + estimated_right_slip
            )
        wall_accuracy = _mean_optional(
            _numbers(payloads, "map_wall_accuracy")
        )
        grid_accuracy = _mean_optional(
            _numbers(payloads, "grid_position_accuracy")
        )
        stop_errors = _numbers(payloads, "stop_error_mm", absolute=True)
        elapsed_s = _elapsed_seconds(
            run["started_at_utc"],
            run["ended_at_utc"],
        )
        completed_moves = sum(
            1
            for event in done_events
            if event["payload"].get("name") == "move_cell"
            and event["payload"].get("success") is not False
        )
        safety_codes = [
            str(payload.get("code") or "")
            for payload in payloads
        ]
        straight_errors = [
            abs(float(event["payload"]["distance_error_mm"]))
            for event in done_events
            if event["payload"].get("name") == "move_cell"
            and _finite_number(
                event["payload"].get("distance_error_mm")
            )
        ]
        turn_errors = [
            abs(float(event["payload"]["turn_error_deg"]))
            for event in done_events
            if str(event["payload"].get("name") or "").startswith(
                "turn_"
            )
            and _finite_number(event["payload"].get("turn_error_deg"))
        ]
        heading_drift = _numbers(
            telemetry,
            "heading_error_deg",
            absolute=True,
        )
        truth_collisions = _nested_numbers(
            telemetry,
            "sim_truth",
            "collision_count",
        )
        collision_errors = sum(
            code
            in {
                "COLLISION",
                "COLLISION_SUSPECTED",
                "OBSTACLE_COLLISION",
            }
            for code in safety_codes
        )
        stall_count = sum(
            code == "MOTOR_STALL" for code in safety_codes
        )
        wheelspin_count = sum(
            code == "WHEELSPIN_PERSISTENT"
            for code in safety_codes
        )
        safety_fault_count = sum(
            code
            in {
                "OBSTACLE_TOO_CLOSE",
                "ACTION_TIMEOUT",
                "HEARTBEAT_TIMEOUT",
                "ESTOP",
                "BOUNDARY_VIOLATION",
                "SENSOR_NONFINITE",
                "DEVICE_MISSING",
            }
            for code in safety_codes
        )
        controller_periods = _numbers(
            telemetry,
            "controller_period_ms",
        )
        realtime_factors = _numbers(
            telemetry,
            "simulation_realtime_factor",
        )
        completion_reason = (
            completion["payload"].get("reason")
            if completion is not None
            else None
        )
        completed = (
            run["status"] == "COMPLETED"
            and (
                completion_reason != "goal_reached"
                or goal_verification is not None
            )
        )
        goal_reached = bool(
            completed
            and completion_reason == "goal_reached"
            and goal_verification is not None
        )
        return {
            "completed": completed,
            "goal_reached": goal_reached,
            "map_wall_accuracy": wall_accuracy,
            "grid_position_accuracy": grid_accuracy,
            "position_rmse_mm": _rmse(position_errors),
            "heading_mae_deg": _mean_optional(heading_errors),
            "straight_distance_error_mm": _mean_optional(
                straight_errors
            ),
            "turn_error_deg": _mean_optional(turn_errors),
            "heading_drift_deg": (
                max(heading_drift) if heading_drift else None
            ),
            "path_length_mm": None,
            "path_length_cells": completed_moves,
            "optimal_path_length_mm": None,
            "elapsed_s": elapsed_s,
            "target_time_s": None,
            "stop_error_p95_mm": _percentile(stop_errors, 0.95),
            "action_success_rate": (
                len(done_events) / attempts if attempts else None
            ),
            "retry_count": len(action_errors),
            "max_slip_rate": max(slip_values) if slip_values else None,
            "avg_slip_rate": _mean_optional(slip_values),
            "estimated_left_slip_rate": _mean_optional(
                estimated_left_slip
            ),
            "estimated_right_slip_rate": _mean_optional(
                estimated_right_slip
            ),
            "truth_left_slip_rate": _mean_optional(
                truth_left_slip
            ),
            "truth_right_slip_rate": _mean_optional(
                truth_right_slip
            ),
            "collision_count": max(
                [collision_errors, *truth_collisions]
            ),
            "stall_count": stall_count,
            "wheelspin_count": wheelspin_count,
            "safety_fault_count": safety_fault_count,
            "mean_controller_period_ms": _mean_optional(
                controller_periods
            ),
            "mean_simulation_realtime_factor": _mean_optional(
                realtime_factors
            ),
            "boundary_violation_count": sum(
                code == "BOUNDARY_VIOLATION" for code in safety_codes
            ),
            "timeout_count": sum(
                code in {"ACTION_TIMEOUT", "HEARTBEAT_TIMEOUT"}
                for code in safety_codes
            ),
            "estop_count": sum(
                event["type"] == "task.estop" for event in events
            ),
            "safety_event_count": sum(
                event["type"].startswith("safety.")
                for event in events
            ),
            "evidence": {
                "event_count": len(events),
                "telemetry_count": len(telemetry),
                "missing_metrics_are_null": True,
            },
        }

    def _require_run(self, run_id: str) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT id, status, started_at_utc, ended_at_utc
                FROM runs
                WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            raise RunMetricsNotFoundError(f"run not found: {run_id}")
        return dict(row)

    def _register_artifact(
        self,
        *,
        run_id: str,
        kind: str,
        relative_path: str,
        sha256: str | None,
        retained_until: datetime,
        metadata: Mapping[str, Any],
    ) -> None:
        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO artifacts (
                    id, run_id, kind, relative_path, sha256,
                    metadata_json, retained_until_utc, pinned,
                    created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(relative_path) DO NOTHING
                """,
                (
                    str(self.id_factory()),
                    run_id,
                    kind,
                    relative_path,
                    sha256,
                    _canonical_json(metadata),
                    _utc_text(retained_until),
                    _utc_text(self.utc_now()),
                ),
            )


def _load_profile(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScoreProfileError(f"invalid score profile: {path}") from exc
    if not isinstance(value, dict):
        raise ScoreProfileError("score profile must be an object")
    version = value.get("profile_version")
    weights = value.get("weights")
    thresholds = value.get("thresholds")
    required = {
        "completion_goal",
        "map_accuracy",
        "localization_heading",
        "path_time",
        "action_precision",
        "safety_stability",
    }
    if not isinstance(version, str) or not version:
        raise ScoreProfileError("profile_version is required")
    if not isinstance(weights, dict) or set(weights) != required:
        raise ScoreProfileError("score profile weights are incomplete")
    if any(not _finite_number(value) or value < 0 for value in weights.values()):
        raise ScoreProfileError("score profile weights must be non-negative")
    if not math.isclose(sum(weights.values()), 100.0):
        raise ScoreProfileError("score profile weights must total 100")
    if not isinstance(thresholds, dict):
        raise ScoreProfileError("score profile thresholds are required")
    return value


def _calculate_breakdown(
    metrics: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    weights = profile["weights"]
    thresholds = profile["thresholds"]
    completion = (
        float(bool(metrics.get("completed")))
        + float(bool(metrics.get("goal_reached")))
    ) / 2.0
    map_accuracy = _ratio(metrics.get("map_wall_accuracy"))
    localization_values = (
        _ratio(metrics.get("grid_position_accuracy")),
        _inverse_threshold(
            metrics.get("position_rmse_mm"),
            thresholds["position_rmse_fail_mm"],
        ),
        _inverse_threshold(
            metrics.get("heading_mae_deg"),
            thresholds["heading_mae_fail_deg"],
        ),
    )
    path_values = (
        _efficiency(
            metrics.get("optimal_path_length_mm"),
            metrics.get("path_length_mm"),
        ),
        _efficiency(
            metrics.get("target_time_s"),
            metrics.get("elapsed_s"),
        ),
    )
    action_values = (
        _ratio(metrics.get("action_success_rate")),
        _inverse_threshold(
            metrics.get("stop_error_p95_mm"),
            thresholds["stop_error_fail_mm"],
        ),
        max(
            0.0,
            1.0
            - max(0.0, _number(metrics.get("retry_count"), 0.0))
            * thresholds["retry_penalty_each"],
        ),
    )
    safety_values = (
        metrics.get("collision_count"),
        metrics.get("boundary_violation_count"),
        metrics.get("timeout_count"),
        metrics.get("estop_count"),
        metrics.get("safety_event_count"),
        metrics.get("max_slip_rate"),
        metrics.get("avg_slip_rate"),
    )
    safety_factor = None
    if all(_finite_number(value) for value in safety_values):
        penalties = (
            safety_values[0] * thresholds["collision_penalty_each"]
            + safety_values[1] * thresholds["boundary_penalty_each"]
            + safety_values[2] * thresholds["timeout_penalty_each"]
            + safety_values[3] * thresholds["estop_penalty_each"]
            + safety_values[4] * thresholds["safety_event_penalty_each"]
            + safety_values[5] * thresholds["max_slip_penalty"]
            + safety_values[6] * thresholds["average_slip_penalty"]
        )
        safety_factor = max(0.0, min(1.0, 1.0 - penalties))
    return {
        "completion_goal": _category(
            weights["completion_goal"],
            completion,
        ),
        "map_accuracy": _category(
            weights["map_accuracy"],
            map_accuracy,
        ),
        "localization_heading": _category(
            weights["localization_heading"],
            _complete_average(localization_values),
        ),
        "path_time": _category(
            weights["path_time"],
            _complete_average(path_values),
        ),
        "action_precision": _category(
            weights["action_precision"],
            _complete_average(action_values),
        ),
        "safety_stability": _category(
            weights["safety_stability"],
            safety_factor,
        ),
    }


def _category(weight: float, factor: float | None) -> dict[str, Any]:
    return {
        "weight": float(weight),
        "factor": None if factor is None else round(factor, 6),
        "score": (
            0.0
            if factor is None
            else round(float(weight) * factor, 4)
        ),
        "evidence_complete": factor is not None,
    }


def _score_row(
    row: Any,
    *,
    run_id: str,
    profile_version: str,
) -> dict[str, Any]:
    stored = json.loads(row["breakdown_json"])
    metrics = json.loads(row["raw_metrics_json"])
    return {
        "score_id": row["id"],
        "run_id": run_id,
        "profile_version": profile_version,
        "raw_metrics_digest": stored.get(
            "raw_metrics_digest",
            _digest(metrics),
        ),
        "breakdown": stored.get("categories", stored),
        "total_score": row["total_score"],
        "created_at_utc": row["created_at_utc"],
    }


def _complete_average(values: tuple[float | None, ...]) -> float | None:
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None) / len(values)


def _efficiency(target: Any, actual: Any) -> float | None:
    if not _finite_number(target) or not _finite_number(actual):
        return None
    if float(target) <= 0 or float(actual) <= 0:
        return None
    return max(0.0, min(1.0, float(target) / float(actual)))


def _inverse_threshold(value: Any, threshold: Any) -> float | None:
    if not _finite_number(value) or not _finite_number(threshold):
        return None
    if float(threshold) <= 0:
        return None
    return max(0.0, min(1.0, 1.0 - abs(float(value)) / float(threshold)))


def _ratio(value: Any) -> float | None:
    if not _finite_number(value):
        return None
    return max(0.0, min(1.0, float(value)))


def _numbers(
    payloads: list[Mapping[str, Any]],
    field: str,
    *,
    scale: float = 1.0,
    absolute: bool = False,
) -> list[float]:
    values = []
    for payload in payloads:
        value = payload.get(field)
        if not _finite_number(value):
            continue
        number = float(value) * scale
        values.append(abs(number) if absolute else number)
    return values


def _nested_numbers(
    payloads: list[Mapping[str, Any]],
    parent: str,
    field: str,
    *,
    absolute: bool = False,
) -> list[float]:
    values = []
    for payload in payloads:
        nested = payload.get(parent)
        if not isinstance(nested, Mapping):
            continue
        value = nested.get(field)
        if not _finite_number(value):
            continue
        number = float(value)
        values.append(abs(number) if absolute else number)
    return values


def _mean_optional(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _rmse(values: list[float]) -> float | None:
    if not values:
        return None
    return math.sqrt(sum(value * value for value in values) / len(values))


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(
        0,
        min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1),
    )
    return ordered[index]


def _elapsed_seconds(start: Any, end: Any) -> float | None:
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (end_dt - start_dt).total_seconds())


def _json_object(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        encoded = _canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"metrics are not valid JSON: {exc}") from exc
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise ValueError("metrics must be a JSON object")
    return decoded


def _digest(value: Any) -> str:
    return hashlib.sha256(
        _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RawMetricsConflictError(
            f"cannot read immutable metrics: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise RawMetricsConflictError(
            f"immutable metrics must be an object: {path}"
        )
    return value


def _validate_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or SAFE_RUN_ID.fullmatch(run_id) is None:
        raise ValueError("run_id contains unsafe characters")


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _number(value: Any, default: float) -> float:
    return float(value) if _finite_number(value) else default


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC clock must return a timezone-aware datetime")
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
