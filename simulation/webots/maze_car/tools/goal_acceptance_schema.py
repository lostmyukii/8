"""Strict, fail-closed schema for full map-goal P5 evidence."""

from __future__ import annotations

import math
import re
from typing import Any, Mapping, Sequence


_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_STATUS = frozenset({"PASS", "FAIL", "unavailable"})
_HEADINGS = frozenset({"N", "E", "S", "W"})
_TURN_ACTIONS = frozenset({"turn_left", "turn_right", "turn_back"})
_EXTERNAL_EVIDENCE = frozenset(
    {
        "tof_front",
        "tof_left",
        "tof_right",
        "wall_constraint",
        "imu",
        "camera_marker",
    }
)
_ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "status",
        "source_commit",
        "webots_version",
        "started_at_utc",
        "ended_at_utc",
        "output_dir",
        "map",
        "param_version",
        "completion_thresholds",
        "physical_profile",
        "start",
        "goal",
        "truth_policy",
        "trials",
        "errors",
        "artifacts",
    }
)
_TRIAL_FIELDS = frozenset(
    {
        "trial_index",
        "status",
        "task_id",
        "run_id",
        "task_status",
        "completion_reason",
        "route",
        "action_count",
        "actions",
        "turn_count",
        "corrections",
        "final_pose",
        "safety",
        "evidence_sources",
        "score",
        "replay",
        "raw_events_jsonl",
    }
)
_THRESHOLDS = frozenset(
    {
        "goal_min_confidence",
        "nominal_position_error_ratio",
        "recoverable_position_error_ratio",
        "nominal_heading_error_deg",
        "recoverable_heading_error_deg",
        "max_recovery_attempts_per_cell",
    }
)


class GoalAcceptanceReportError(ValueError):
    """Raised when evidence cannot prove a complete P5 pass."""


def validate_goal_acceptance_report(
    report: Mapping[str, Any],
) -> dict[str, Any]:
    value = _mapping(report, "report")
    _require_fields(value, _ROOT_FIELDS, "acceptance report")
    if value["schema_version"] != 1:
        raise GoalAcceptanceReportError("schema_version must be 1")
    status = _status(value["status"], "status")
    _text(value["run_id"], "run_id")
    commit = _text(value["source_commit"], "source_commit")
    if commit != "unknown" and _COMMIT.fullmatch(commit) is None:
        raise GoalAcceptanceReportError(
            "source_commit must be a Git commit or unknown"
        )
    _text(value["webots_version"], "webots_version")
    _text(value["started_at_utc"], "started_at_utc")
    _text(value["ended_at_utc"], "ended_at_utc")
    _text(value["output_dir"], "output_dir")

    map_record = _mapping(value["map"], "map")
    _text(map_record.get("map_version_id"), "map.map_version_id")
    _digest(map_record.get("digest"), "map.digest")
    params = _mapping(value["param_version"], "param_version")
    _text(params.get("version_id"), "param_version.version_id")
    _digest(params.get("digest"), "param_version.digest")
    thresholds = _mapping(
        value["completion_thresholds"],
        "completion_thresholds",
    )
    _require_fields(thresholds, _THRESHOLDS, "completion thresholds")
    for name in _THRESHOLDS:
        _finite(thresholds[name], f"completion_thresholds.{name}")

    profile = _mapping(value["physical_profile"], "physical_profile")
    _text(profile.get("profile_id"), "physical_profile.profile_id")
    _digest(profile.get("digest"), "physical_profile.digest")
    _integer(profile.get("seed"), "physical_profile.seed")
    _require_pose_cell(value["start"], "start", expected=[0, 4])
    start = _mapping(value["start"], "start")
    if start.get("heading") != "N":
        raise GoalAcceptanceReportError("start heading must be N")
    _require_pose_cell(value["goal"], "goal", expected=[4, 0])
    goal = _mapping(value["goal"], "goal")
    if goal.get("source") != "map_primary_goal":
        raise GoalAcceptanceReportError(
            "automatic goal must come from map_primary_goal"
        )
    truth = _mapping(value["truth_policy"], "truth_policy")
    if (
        truth.get("sim_truth") != "evaluation_only"
        or truth.get("algorithm_evidence_excludes_sim_truth") is not True
    ):
        raise GoalAcceptanceReportError(
            "sim_truth must be evaluation-only"
        )

    trials = _sequence(value["trials"], "trials")
    if status == "PASS" and len(trials) != 2:
        raise GoalAcceptanceReportError(
            "PASS requires exactly two fixed-input trials"
        )
    for index, trial in enumerate(trials):
        _validate_trial(
            trial,
            index=index,
            require_pass=status == "PASS",
        )
    _sequence(value["errors"], "errors")
    artifacts = _mapping(value["artifacts"], "artifacts")
    _text(artifacts.get("report_json"), "artifacts.report_json")
    _text(artifacts.get("events_jsonl"), "artifacts.events_jsonl")
    return dict(value)


def _validate_trial(
    raw: object,
    *,
    index: int,
    require_pass: bool,
) -> None:
    trial = _mapping(raw, f"trials[{index}]")
    _require_fields(trial, _TRIAL_FIELDS, f"trials[{index}]")
    trial_status = _status(trial["status"], f"trials[{index}].status")
    _integer(trial["trial_index"], "trial_index")
    _text(trial["task_id"], "task_id")
    _text(trial["run_id"], "run_id")
    _text(trial["raw_events_jsonl"], "raw_events_jsonl")
    if require_pass and trial_status != "PASS":
        raise GoalAcceptanceReportError("every PASS trial must pass")
    if require_pass and (
        trial["task_status"] != "COMPLETED"
        or trial["completion_reason"] != "goal_reached"
    ):
        raise GoalAcceptanceReportError(
            "trial must end COMPLETED / goal_reached"
        )

    route = _sequence(trial["route"], "route")
    if len(route) < 2:
        raise GoalAcceptanceReportError("route must contain start and goal")
    if list(route[0]) != [0, 4] or list(route[-1]) != [4, 0]:
        raise GoalAcceptanceReportError(
            "route must run from (0,4) to (4,0)"
        )
    for route_index, cell in enumerate(route):
        _cell(cell, f"route[{route_index}]")

    actions = _sequence(trial["actions"], "actions")
    count = _integer(trial["action_count"], "action_count")
    if count != len(actions) or count <= 0:
        raise GoalAcceptanceReportError(
            "action_count must match non-empty action evidence"
        )
    action_ids: set[str] = set()
    turns = 0
    for action_index, raw_action in enumerate(actions):
        item = _mapping(raw_action, f"actions[{action_index}]")
        action_id = _text(item.get("action_id"), "action_id")
        if action_id in action_ids:
            raise GoalAcceptanceReportError(
                f"duplicate action_id: {action_id}"
            )
        action_ids.add(action_id)
        name = _text(item.get("name"), "action.name")
        turns += int(name in _TURN_ACTIONS)
        terminal = _mapping(item.get("terminal"), "action.terminal")
        if terminal.get("type") not in {"done", "error"}:
            raise GoalAcceptanceReportError(
                "every action requires done or error"
            )
        if terminal.get("action_id") != action_id:
            raise GoalAcceptanceReportError(
                "terminal action_id must match action"
            )
        if require_pass and terminal.get("type") != "done":
            raise GoalAcceptanceReportError(
                "PASS cannot contain action error"
            )
    reported_turns = _integer(trial["turn_count"], "turn_count")
    if turns < 1 or reported_turns != turns:
        raise GoalAcceptanceReportError(
            "trial must contain and count at least one turn"
        )

    corrections = _sequence(trial["corrections"], "corrections")
    if not corrections:
        raise GoalAcceptanceReportError(
            "trial requires correction evidence"
        )
    for correction_index, raw_correction in enumerate(corrections):
        correction = _mapping(
            raw_correction,
            f"corrections[{correction_index}]",
        )
        action_id = _text(
            correction.get("action_id"),
            "correction.action_id",
        )
        if action_id not in action_ids:
            raise GoalAcceptanceReportError(
                "correction action must exist in actions"
            )
        _text(correction.get("kind"), "correction.kind")
        before = _mapping(
            correction.get("before_error"),
            "correction.before_error",
        )
        after = _mapping(
            correction.get("after_error"),
            "correction.after_error",
        )
        before_position = _finite(
            before.get("position_error_ratio"),
            "before position error",
        )
        after_position = _finite(
            after.get("position_error_ratio"),
            "after position error",
        )
        before_heading = _finite(
            before.get("heading_error_deg"),
            "before heading error",
        )
        after_heading = _finite(
            after.get("heading_error_deg"),
            "after heading error",
        )
        if (
            after_position > before_position
            or after_heading > before_heading
            or (
                after_position == before_position
                and after_heading == before_heading
            )
        ):
            raise GoalAcceptanceReportError(
                "correction must reduce position or heading error"
            )

    pose = _mapping(trial["final_pose"], "final_pose")
    if list(_cell(pose.get("reliable_cell"), "reliable_cell")) != [4, 0]:
        raise GoalAcceptanceReportError(
            "final reliable cell must be (4,0)"
        )
    for name in ("x_mm", "y_mm", "yaw_deg", "confidence"):
        _finite(pose.get(name), f"final_pose.{name}")
    if pose.get("heading") not in _HEADINGS:
        raise GoalAcceptanceReportError("final heading is invalid")
    confidence = float(pose["confidence"])
    if not 0.0 <= confidence <= 1.0:
        raise GoalAcceptanceReportError(
            "final confidence must be in [0,1]"
        )

    safety = _mapping(trial["safety"], "safety")
    for name in (
        "truth_sample_count",
        "collision_count",
        "out_of_bounds_count",
        "wall_crossing_count",
        "conflict_count",
    ):
        value = _integer(safety.get(name), f"safety.{name}")
        if name == "truth_sample_count":
            if value <= 0:
                raise GoalAcceptanceReportError(
                    "PASS requires simulation evaluation samples"
                )
            continue
        if value != 0:
            raise GoalAcceptanceReportError(
                f"PASS requires safety.{name} == 0"
            )
    sources = {
        _text(source, "evidence source")
        for source in _sequence(
            trial["evidence_sources"],
            "evidence_sources",
        )
    }
    if "sim_truth" in sources:
        raise GoalAcceptanceReportError(
            "algorithm evidence cannot use sim_truth"
        )
    if not sources.intersection(_EXTERNAL_EVIDENCE):
        raise GoalAcceptanceReportError(
            "logical advance requires external pose evidence"
        )
    _mapping(trial["score"], "score")
    replay = _mapping(trial["replay"], "replay")
    _integer(replay.get("schema_version"), "replay.schema_version")
    _text(replay.get("relative_path"), "replay.relative_path")


def _require_pose_cell(
    value: object,
    name: str,
    *,
    expected: list[int],
) -> None:
    item = _mapping(value, name)
    if list(_cell(item.get("cell"), f"{name}.cell")) != expected:
        raise GoalAcceptanceReportError(
            f"{name} cell must be {tuple(expected)}"
        )


def _require_fields(
    value: Mapping[str, Any],
    required: frozenset[str],
    name: str,
) -> None:
    missing = required - set(value)
    if missing:
        raise GoalAcceptanceReportError(
            f"{name} is missing fields: {sorted(missing)}"
        )


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GoalAcceptanceReportError(f"{name} must be an object")
    return value


def _sequence(value: object, name: str) -> Sequence[Any]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
    ):
        raise GoalAcceptanceReportError(f"{name} must be a list")
    return value


def _cell(value: object, name: str) -> tuple[int, int]:
    items = _sequence(value, name)
    if (
        len(items) != 2
        or any(
            not isinstance(item, int) or isinstance(item, bool)
            for item in items
        )
    ):
        raise GoalAcceptanceReportError(
            f"{name} must contain two integers"
        )
    return int(items[0]), int(items[1])


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GoalAcceptanceReportError(
            f"{name} must be non-empty text"
        )
    return value.strip()


def _status(value: object, name: str) -> str:
    text = _text(value, name)
    if text not in _STATUS:
        raise GoalAcceptanceReportError(f"{name} is invalid: {text}")
    return text


def _digest(value: object, name: str) -> str:
    text = _text(value, name)
    if _DIGEST.fullmatch(text) is None:
        raise GoalAcceptanceReportError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return text


def _integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise GoalAcceptanceReportError(f"{name} must be an integer")
    return int(value)


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise GoalAcceptanceReportError(f"{name} must be finite")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise GoalAcceptanceReportError(
            f"{name} must be finite"
        ) from exc
    if not math.isfinite(number):
        raise GoalAcceptanceReportError(f"{name} must be finite")
    return number
