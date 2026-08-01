"""Strict schema checks for archived physical-acceptance reports."""

from __future__ import annotations

import math
import re
from typing import Any, Mapping, Sequence


_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_STATUSES = frozenset({"PASS", "FAIL", "unavailable"})
_REQUIRED_ROOT = frozenset(
    {
        "schema_version",
        "run_id",
        "status",
        "source_commit",
        "webots_version",
        "started_at_utc",
        "ended_at_utc",
        "output_dir",
        "profiles",
        "maps",
        "p1",
        "p2",
        "scenarios",
        "performance",
        "errors",
        "artifacts",
    }
)
_REQUIRED_STAGE = frozenset({"status", "metrics", "thresholds"})
_REQUIRED_SCENARIO = frozenset(
    {
        "scenario_id",
        "status",
        "profile_id",
        "profile_digest",
        "map_version_id",
        "map_digest",
        "seed",
        "metrics",
        "thresholds",
        "errors",
    }
)


class AcceptanceReportError(ValueError):
    """Raised when a report cannot prove a complete acceptance run."""


def validate_acceptance_report(
    report: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate required evidence without silently filling omissions."""

    value = _mapping(report, "report")
    missing = _REQUIRED_ROOT - set(value)
    if missing:
        raise AcceptanceReportError(
            f"acceptance report is missing fields: {sorted(missing)}"
        )
    if value["schema_version"] != 1:
        raise AcceptanceReportError("schema_version must be 1")
    status = _status(value["status"], "status")
    _text(value["run_id"], "run_id")
    commit = _text(value["source_commit"], "source_commit")
    if commit != "unknown" and _COMMIT.fullmatch(commit) is None:
        raise AcceptanceReportError(
            "source_commit must be a Git commit or unknown"
        )
    _text(value["webots_version"], "webots_version")
    _text(value["started_at_utc"], "started_at_utc")
    _text(value["ended_at_utc"], "ended_at_utc")
    _text(value["output_dir"], "output_dir")

    profiles = _sequence(value["profiles"], "profiles")
    maps = _sequence(value["maps"], "maps")
    scenarios = _sequence(value["scenarios"], "scenarios")
    if status == "PASS" and (not profiles or not maps or not scenarios):
        raise AcceptanceReportError(
            "PASS requires profiles, maps, and scenario evidence"
        )
    for index, profile in enumerate(profiles):
        item = _mapping(profile, f"profiles[{index}]")
        _text(item.get("profile_id"), "profile_id")
        _digest(item.get("digest"), "profile digest")
        _integer(item.get("seed"), "profile seed")
    for index, map_record in enumerate(maps):
        item = _mapping(map_record, f"maps[{index}]")
        _text(item.get("map_version_id"), "map_version_id")
        _digest(item.get("digest"), "map digest")

    _validate_stage(value["p1"], "p1")
    _validate_stage(value["p2"], "p2")
    for index, scenario in enumerate(scenarios):
        item = _mapping(scenario, f"scenarios[{index}]")
        missing_scenario = _REQUIRED_SCENARIO - set(item)
        if missing_scenario:
            raise AcceptanceReportError(
                "scenario report is missing fields: "
                f"{sorted(missing_scenario)}"
            )
        _status(item["status"], "scenario.status")
        _text(item["scenario_id"], "scenario_id")
        _text(item["profile_id"], "profile_id")
        _digest(item["profile_digest"], "profile_digest")
        _text(item["map_version_id"], "map_version_id")
        _digest(item["map_digest"], "map_digest")
        _integer(item["seed"], "seed")
        _mapping(item["metrics"], "scenario.metrics")
        _mapping(item["thresholds"], "scenario.thresholds")
        _sequence(item["errors"], "scenario.errors")

    performance = _mapping(value["performance"], "performance")
    for name in ("real_time_factor", "controller_period_ms"):
        if performance.get(name) is not None:
            _finite(performance[name], f"performance.{name}")
    _sequence(value["errors"], "errors")
    artifacts = _mapping(value["artifacts"], "artifacts")
    _text(artifacts.get("events_jsonl"), "artifacts.events_jsonl")
    _text(artifacts.get("report_json"), "artifacts.report_json")
    return dict(value)


def _validate_stage(value: object, name: str) -> None:
    item = _mapping(value, name)
    missing = _REQUIRED_STAGE - set(item)
    if missing:
        raise AcceptanceReportError(
            f"{name} is missing fields: {sorted(missing)}"
        )
    _status(item["status"], f"{name}.status")
    _mapping(item["metrics"], f"{name}.metrics")
    _mapping(item["thresholds"], f"{name}.thresholds")


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AcceptanceReportError(f"{name} must be an object")
    return value


def _sequence(value: object, name: str) -> Sequence[Any]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
    ):
        raise AcceptanceReportError(f"{name} must be a list")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AcceptanceReportError(f"{name} must be non-empty text")
    return value.strip()


def _status(value: object, name: str) -> str:
    text = _text(value, name)
    if text not in _STATUSES:
        raise AcceptanceReportError(f"{name} is invalid: {text}")
    return text


def _digest(value: object, name: str) -> str:
    text = _text(value, name)
    if _DIGEST.fullmatch(text) is None:
        raise AcceptanceReportError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return text


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AcceptanceReportError(f"{name} must be an integer")
    return value


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise AcceptanceReportError(f"{name} must be finite")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AcceptanceReportError(f"{name} must be finite") from exc
    if not math.isfinite(number):
        raise AcceptanceReportError(f"{name} must be finite")
    return number
