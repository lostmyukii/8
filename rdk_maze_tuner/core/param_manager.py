"""Parameter loading, validation, and ESP32 export."""

from __future__ import annotations

import ast
import copy
from pathlib import Path
from typing import Any, Dict, Mapping, MutableMapping


class ParamValidationError(ValueError):
    """Raised when a parameter update violates known limits."""


class ParamManager:
    ESP32_EXPORTS = {
        "motor.base_speed": "base_speed",
        "motor.turn_speed": "turn_speed",
        "motor.max_speed": "max_speed",
        "motor.min_pwm_left": "min_pwm_left",
        "motor.min_pwm_right": "min_pwm_right",
        "motor.max_pwm": "max_pwm",
        "motor.left_trim": "left_trim",
        "motor.right_trim": "right_trim",
        "motion.cell_ticks": "cell_ticks",
        "motion.turn_90_ticks": "turn_90_ticks",
        "motion.turn_180_ticks": "turn_180_ticks",
        "motion.brake_ticks": "brake_ticks",
        "motion.stop_tolerance_ticks": "stop_tolerance_ticks",
        "pid.speed_kp": "speed_kp",
        "pid.speed_ki": "speed_ki",
        "pid.speed_kd": "speed_kd",
        "pid.heading_kp": "heading_kp",
        "pid.heading_kd": "heading_kd",
        "tof.wall_threshold_mm": "wall_threshold_mm",
        "tof.open_threshold_mm": "open_threshold_mm",
        "tof.front_stop_mm": "front_stop_mm",
        "tof.danger_stop_mm": "danger_stop_mm",
        "tof.filter_window": "filter_window",
        "wall_follow.enabled": "wall_follow_enabled",
        "wall_follow.center_kp": "center_kp",
        "wall_follow.center_max_correction": "center_max_correction",
        "safety.heartbeat_timeout_ms": "heartbeat_timeout_ms",
        "safety.action_timeout_ms": "action_timeout_ms",
    }

    def __init__(self, *, params_path: Path, limits_path: Path) -> None:
        self.params_path = params_path
        self.limits_path = limits_path
        self.params = _load_mapping(params_path)
        self.limits = _load_mapping(limits_path)
        self.param_version = 1

    def get(self, dotted_path: str) -> Any:
        node: Any = self.params
        for part in dotted_path.split("."):
            if not isinstance(node, Mapping) or part not in node:
                raise KeyError(dotted_path)
            node = node[part]
        return node

    def apply_updates(self, updates: Mapping[str, Any], *, source: str) -> Dict[str, Any]:
        changes: Dict[str, list[Any]] = {}
        next_params = copy.deepcopy(self.params)

        for dotted_path, new_value in updates.items():
            old_value = self.get(dotted_path)
            self._validate(dotted_path, new_value)
            self._set(next_params, dotted_path, new_value)
            if old_value != new_value:
                changes[dotted_path] = [old_value, new_value]

        if changes:
            self.params = next_params
            self.param_version += 1

        return {
            "type": "param_change",
            "source": source,
            "param_version": self.param_version,
            "changes": changes,
        }

    def esp32_params(self) -> Dict[str, Any]:
        exported: Dict[str, Any] = {}
        for dotted_path, esp32_name in self.ESP32_EXPORTS.items():
            try:
                exported[esp32_name] = self.get(dotted_path)
            except KeyError:
                continue
        exported["param_version"] = self.param_version
        return exported

    def snapshot(self) -> Dict[str, Any]:
        return {
            "param_version": self.param_version,
            "params": copy.deepcopy(self.params),
            "esp32_params": self.esp32_params(),
        }

    def _validate(self, dotted_path: str, value: Any) -> None:
        if not _path_exists(self.params, dotted_path):
            raise ParamValidationError(f"{dotted_path} is not a known parameter")
        if dotted_path not in self.limits:
            return
        bounds = self.limits[dotted_path]
        if not isinstance(bounds, list) or len(bounds) != 2:
            raise ParamValidationError(f"{dotted_path} has invalid limit definition")
        lower, upper = bounds
        if value < lower or value > upper:
            raise ParamValidationError(f"{dotted_path}={value!r} outside [{lower!r}, {upper!r}]")

    def _set(self, root: MutableMapping[str, Any], dotted_path: str, value: Any) -> None:
        node: MutableMapping[str, Any] = root
        parts = dotted_path.split(".")
        for part in parts[:-1]:
            child = node[part]
            if not isinstance(child, MutableMapping):
                raise ParamValidationError(f"{dotted_path} cannot be assigned")
            node = child
        node[parts[-1]] = value


def _path_exists(root: Mapping[str, Any], dotted_path: str) -> bool:
    node: Any = root
    for part in dotted_path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return False
        node = node[part]
    return True


def _load_mapping(path: Path) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except ModuleNotFoundError:
        data = _load_simple_yaml(path)
    if not isinstance(data, dict):
        raise ParamValidationError(f"{path} must contain a mapping")
    return data


def _load_simple_yaml(path: Path) -> Dict[str, Any]:
    root: Dict[str, Any] = {}
    current_section: Dict[str, Any] | None = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line:
            continue
        if not line.startswith(" "):
            key, value = _split_key_value(line)
            if value is None:
                current_section = {}
                root[key] = current_section
            else:
                root[key] = _parse_scalar(value)
                current_section = None
            continue
        if current_section is None:
            raise ParamValidationError(f"invalid indentation in {path}: {raw_line}")
        key, value = _split_key_value(line.strip())
        if value is None:
            raise ParamValidationError(f"nested sections deeper than one level are not supported: {raw_line}")
        current_section[key] = _parse_scalar(value)
    return root


def _split_key_value(line: str) -> tuple[str, str | None]:
    if ":" not in line:
        raise ParamValidationError(f"invalid YAML line: {line}")
    key, value = line.split(":", 1)
    key = key.strip()
    value = value.strip()
    return key, value or None


def _parse_scalar(value: str) -> Any:
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return value.strip("\"'")
