"""Motion result analysis for rule-based tuning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Tuple

from .param_manager import ParamManager


@dataclass(frozen=True)
class MotionReport:
    action_id: str
    name: str
    success: bool
    target_ticks: int
    average_ticks: int
    distance_error_ticks: int
    encoder_delta: int
    left_right_ratio: float
    duration_ms: int
    front_mm: int | None
    left_mm: int | None
    right_mm: int | None
    issues: Tuple[str, ...]
    confidence: float


class MotionAnalyzer:
    def __init__(self, params: ParamManager) -> None:
        self.params = params

    def analyze(self, *, action_name: str, target_ticks: int, result: Mapping[str, Any]) -> MotionReport:
        enc_left = int(result.get("enc_left", 0))
        enc_right = int(result.get("enc_right", 0))
        abs_left = abs(enc_left)
        abs_right = abs(enc_right)
        average_ticks = int(round((abs_left + abs_right) / 2))
        distance_error_ticks = average_ticks - int(target_ticks)
        encoder_delta = enc_left - enc_right
        left_right_ratio = _safe_ratio(abs_left, abs_right)
        success = bool(result.get("success", result.get("type") != "error"))
        issues = []

        if result.get("code") == "OBSTACLE_TOO_CLOSE":
            issues.append("obstacle_too_close")

        tolerance = int(self.params.get("motion.stop_tolerance_ticks"))
        if action_name == "move_cell":
            if abs(encoder_delta) > tolerance:
                issues.append("drift_right" if encoder_delta > 0 else "drift_left")
            if distance_error_ticks < -tolerance:
                issues.append("move_short")
            elif distance_error_ticks > tolerance:
                issues.append("move_long")

            front_mm = _optional_int(result.get("front_mm"))
            if front_mm is not None and front_mm < int(self.params.get("tof.danger_stop_mm")):
                if "obstacle_too_close" not in issues:
                    issues.append("obstacle_too_close")
        elif action_name.startswith("turn_"):
            if distance_error_ticks > tolerance:
                issues.append("turn_overshoot")
            elif distance_error_ticks < -tolerance:
                issues.append("turn_undershoot")

        confidence = min(0.95, 0.35 + 0.15 * len(issues))
        return MotionReport(
            action_id=str(result.get("action_id", "")),
            name=str(result.get("name", action_name)),
            success=success,
            target_ticks=int(target_ticks),
            average_ticks=average_ticks,
            distance_error_ticks=distance_error_ticks,
            encoder_delta=encoder_delta,
            left_right_ratio=left_right_ratio,
            duration_ms=int(result.get("duration_ms", 0)),
            front_mm=_optional_int(result.get("front_mm")),
            left_mm=_optional_int(result.get("left_mm")),
            right_mm=_optional_int(result.get("right_mm")),
            issues=tuple(issues),
            confidence=confidence,
        )


def _safe_ratio(left: int, right: int) -> float:
    if right == 0:
        return float("inf") if left else 1.0
    return round(left / right, 4)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)

