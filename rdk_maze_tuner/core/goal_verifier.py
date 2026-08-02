"""Pure verification of physically evidenced arrival at a map-owned goal."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from .motion_evidence import ArrivalVerificationConfig


@dataclass(frozen=True)
class GoalVerificationInput:
    logical_cell: tuple[int, int]
    last_action_id: str | None
    last_result: Mapping[str, Any] | None
    reliable_pose: Mapping[str, Any] | None
    unresolved_faults: tuple[str, ...] = ()


@dataclass(frozen=True)
class GoalVerificationDecision:
    verified: bool
    code: str | None
    reasons: tuple[str, ...]
    checks: Mapping[str, bool]
    goal: Mapping[str, Any]
    position_error_mm: float | None
    position_tolerance_mm: float
    pose_confidence: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "code": self.code,
            "reasons": list(self.reasons),
            "checks": dict(self.checks),
            "goal": dict(self.goal),
            "position_error_mm": self.position_error_mm,
            "position_tolerance_mm": self.position_tolerance_mm,
            "pose_confidence": self.pose_confidence,
        }


class GoalVerifier:
    """Evaluate immutable task evidence without changing task state."""

    def __init__(
        self,
        *,
        goal: Mapping[str, Any],
        map_version_id: str | None,
        map_digest: str | None,
        cell_width_mm: float,
        cell_height_mm: float,
        config: ArrivalVerificationConfig,
    ) -> None:
        self.goal = dict(goal)
        self.map_version_id = str(map_version_id or "")
        self.map_digest = str(map_digest or "")
        self.cell_width_mm = _positive(
            cell_width_mm,
            "cell_width_mm",
        )
        self.cell_height_mm = _positive(
            cell_height_mm,
            "cell_height_mm",
        )
        self.config = config
        cell = self.goal.get("cell")
        if (
            self.goal.get("type") != "map_goal"
            or not isinstance(cell, (list, tuple))
            or len(cell) != 2
            or any(
                not isinstance(value, int) or isinstance(value, bool)
                for value in cell
            )
        ):
            raise ValueError(
                "goal must be a map_goal with an integer cell"
            )
        self.goal_cell = (int(cell[0]), int(cell[1]))
        self.position_tolerance_mm = (
            min(self.cell_width_mm, self.cell_height_mm)
            * config.nominal_position_error_ratio
        )

    def verify(
        self,
        evidence: GoalVerificationInput,
    ) -> GoalVerificationDecision:
        pose = (
            evidence.reliable_pose
            if isinstance(evidence.reliable_pose, Mapping)
            else {}
        )
        result = (
            evidence.last_result
            if isinstance(evidence.last_result, Mapping)
            else {}
        )
        pose_cell = _cell(pose.get("grid_cell"))
        position_error = self._position_error(pose)
        confidence = _finite(pose.get("confidence"))
        checks = {
            "map_version_matches": (
                str(self.goal.get("source_map_version") or "")
                == self.map_version_id
            ),
            "map_digest_matches": (
                str(self.goal.get("source_map_digest") or "")
                == self.map_digest
            ),
            "logical_cell_matches": (
                tuple(evidence.logical_cell) == self.goal_cell
            ),
            "matching_done": (
                bool(evidence.last_action_id)
                and result.get("type") == "done"
                and result.get("action_id") == evidence.last_action_id
                and result.get("success") is not False
            ),
            "reliable_cell_matches": pose_cell == self.goal_cell,
            "continuous_pose_within_tolerance": (
                position_error is not None
                and position_error <= self.position_tolerance_mm
            ),
            "confidence_sufficient": (
                confidence is not None
                and confidence >= self.config.goal_min_confidence
            ),
            "fault_free": not evidence.unresolved_faults,
        }
        ordered_failures = (
            (
                "map_version_matches",
                "MAP_VERSION_MISMATCH",
                "goal source map version does not match the active map",
            ),
            (
                "map_digest_matches",
                "MAP_DIGEST_MISMATCH",
                "goal source map digest does not match the active map",
            ),
            (
                "logical_cell_matches",
                "LOGICAL_GOAL_MISMATCH",
                "logical maze cell is not the map-owned goal",
            ),
            (
                "matching_done",
                "ACTION_DONE_MISSING",
                "latest action lacks a matching successful done",
            ),
            (
                "reliable_cell_matches",
                "RELIABLE_GOAL_MISMATCH",
                "reliable fused cell is not the map-owned goal",
            ),
            (
                "continuous_pose_within_tolerance",
                "GOAL_POSITION_OUT_OF_TOLERANCE",
                "continuous fused pose is outside the goal tolerance",
            ),
            (
                "confidence_sufficient",
                "POSE_UNCERTAIN",
                "pose confidence is below the frozen goal threshold",
            ),
            (
                "fault_free",
                "GOAL_BLOCKED_BY_FAULT",
                "an unresolved safety or transport fault blocks completion",
            ),
        )
        code = None
        reasons = ()
        for check, failure_code, reason in ordered_failures:
            if not checks[check]:
                code = failure_code
                reasons = (reason,)
                break
        return GoalVerificationDecision(
            verified=code is None,
            code=code,
            reasons=reasons,
            checks=checks,
            goal={
                "cell": [self.goal_cell[0], self.goal_cell[1]],
                "source_map_version": self.goal.get(
                    "source_map_version"
                ),
                "source_map_digest": self.goal.get(
                    "source_map_digest"
                ),
            },
            position_error_mm=position_error,
            position_tolerance_mm=self.position_tolerance_mm,
            pose_confidence=confidence,
        )

    def _position_error(
        self,
        pose: Mapping[str, Any],
    ) -> float | None:
        x = _finite(pose.get("x_mm"))
        y = _finite(pose.get("y_mm"))
        if x is None or y is None:
            return None
        goal_x = (self.goal_cell[0] + 0.5) * self.cell_width_mm
        goal_y = (self.goal_cell[1] + 0.5) * self.cell_height_mm
        return round(math.hypot(x - goal_x, y - goal_y), 6)


def _cell(value: Any) -> tuple[int, int] | None:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(
            not isinstance(item, int) or isinstance(item, bool)
            for item in value
        )
    ):
        return None
    return int(value[0]), int(value[1])


def _positive(value: Any, name: str) -> float:
    number = _finite(value)
    if number is None or number <= 0:
        raise ValueError(f"{name} must be positive")
    return number


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
