"""Rule-based automatic tuning constrained by ParamManager limits."""

from __future__ import annotations

from typing import Any, Dict

from .motion_analyzer import MotionReport
from .param_manager import ParamManager


class AutoTuner:
    TUNABLE_PATHS = frozenset(
        {
            "motor.base_speed",
            "motor.turn_speed",
            "motor.left_trim",
            "motor.right_trim",
            "motion.cell_ticks",
            "motion.turn_90_ticks",
            "tof.front_stop_mm",
        }
    )

    def __init__(self, params: ParamManager) -> None:
        self.params = params

    def propose(self, report: MotionReport) -> Dict[str, Any]:
        if not bool(self.params.get("auto_tune.enabled")):
            return {}

        updates: Dict[str, Any] = {}
        max_params = int(self.params.get("auto_tune.max_params_per_step"))

        for issue in report.issues:
            for path, value in self._updates_for_issue(issue).items():
                if path not in self.TUNABLE_PATHS:
                    continue
                if path in updates:
                    continue
                updates[path] = self._clamp_to_limit(path, value)
                if len(updates) >= max_params:
                    return updates
        return updates

    def apply(self, report: MotionReport) -> Dict[str, Any]:
        updates = self.propose(report)
        if not updates:
            return {
                "type": "param_change",
                "source": "auto_tune",
                "reason": ",".join(report.issues),
                "param_version": self.params.param_version,
                "changes": {},
            }
        event = self.params.apply_updates(updates, source="auto_tune")
        event["reason"] = ",".join(report.issues)
        return event

    def _updates_for_issue(self, issue: str) -> Dict[str, Any]:
        if issue == "drift_left":
            return {
                "motor.right_trim": self.params.get("motor.right_trim") * 0.98,
                "motor.left_trim": self.params.get("motor.left_trim") * 1.01,
            }
        if issue == "drift_right":
            return {
                "motor.left_trim": self.params.get("motor.left_trim") * 0.98,
                "motor.right_trim": self.params.get("motor.right_trim") * 1.01,
            }
        if issue == "move_short":
            return {"motion.cell_ticks": self.params.get("motion.cell_ticks") * 1.03}
        if issue == "move_long":
            return {"motion.cell_ticks": self.params.get("motion.cell_ticks") * 0.97}
        if issue == "turn_overshoot":
            return {
                "motion.turn_90_ticks": self.params.get("motion.turn_90_ticks") * 0.96,
                "motor.turn_speed": self.params.get("motor.turn_speed") * 0.95,
            }
        if issue == "turn_undershoot":
            return {"motion.turn_90_ticks": self.params.get("motion.turn_90_ticks") * 1.04}
        if issue == "obstacle_too_close":
            return {
                "tof.front_stop_mm": self.params.get("tof.front_stop_mm") + 10,
                "motor.base_speed": self.params.get("motor.base_speed") * 0.92,
            }
        return {}

    def _clamp_to_limit(self, path: str, value: Any) -> Any:
        value = self._normalize_value(path, value)
        bounds = self.params.limits.get(path)
        if not bounds:
            return value
        lower, upper = bounds
        return min(max(value, lower), upper)

    def _normalize_value(self, path: str, value: Any) -> Any:
        current = self.params.get(path)
        if isinstance(current, int) and not isinstance(current, bool):
            return int(round(value))
        if isinstance(current, float):
            return round(float(value), 4)
        return value
