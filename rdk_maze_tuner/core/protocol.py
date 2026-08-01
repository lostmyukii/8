"""Newline-delimited JSON protocol helpers for RDK X3 <-> ESP32."""

from __future__ import annotations

import json
import math
from typing import Any, Dict, Mapping


Message = Dict[str, Any]

FUSION_TELEMETRY_FIELDS = frozenset(
    {
        "ts_ms",
        "uptime_ms",
        "enc_left",
        "enc_right",
        "front_mm",
        "left_mm",
        "right_mm",
        "fusion_front_mm",
        "fusion_left_mm",
        "fusion_right_mm",
        "imu_available",
        "imu_yaw_deg",
        "yaw_rate_dps",
        "accel_forward_mps2",
        "quality_flags",
    }
)


class ProtocolError(ValueError):
    """Raised when a serial protocol frame is malformed."""


def encode_message(message: Mapping[str, Any]) -> bytes:
    """Encode one JSON object as a compact UTF-8 line."""
    if not isinstance(message, Mapping):
        raise ProtocolError("message must be an object")
    payload = json.dumps(dict(message), ensure_ascii=False, separators=(",", ":"))
    return f"{payload}\n".encode("utf-8")


def decode_line(line: bytes | str) -> Message:
    """Decode one JSON line from the ESP32."""
    if isinstance(line, bytes):
        raw = line.decode("utf-8", errors="strict")
    else:
        raw = line
    raw = raw.strip()
    if not raw:
        raise ProtocolError("empty protocol line")
    try:
        message = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("protocol payload must be an object")
    return message


def build_heartbeat(*, seq: int, ts_ms: int) -> Message:
    return {"type": "heartbeat", "seq": int(seq), "ts_ms": int(ts_ms)}


def build_set_params(*, seq: int, params: Mapping[str, Any]) -> Message:
    return {"type": "set_params", "seq": int(seq), "params": dict(params)}


def build_action(*, seq: int, action_id: str, name: str, speed: float, target_ticks: int) -> Message:
    return {
        "type": "action",
        "seq": int(seq),
        "action_id": action_id,
        "name": name,
        "speed": float(speed),
        "target_ticks": int(target_ticks),
    }


def build_stop(*, seq: int) -> Message:
    return {"type": "stop", "seq": int(seq)}


def build_estop(*, seq: int, reason: str) -> Message:
    return {"type": "estop", "seq": int(seq), "reason": reason}


def extract_fusion_telemetry(
    message: Mapping[str, Any],
) -> Message:
    """Return only sensor evidence permitted to enter pose fusion."""
    if not isinstance(message, Mapping):
        raise ProtocolError("telemetry must be an object")
    return {
        key: value
        for key, value in message.items()
        if key in FUSION_TELEMETRY_FIELDS
    }


def extract_simulation_truth(
    message: Mapping[str, Any],
) -> Message | None:
    """Read Webots truth through an evaluation-only channel."""
    truth = message.get("sim_truth")
    if truth is None:
        return None
    if not isinstance(truth, Mapping):
        raise ProtocolError("sim_truth must be an object")
    required = ("x_mm", "y_mm", "yaw_deg")
    result: Message = {}
    for key in required:
        value = truth.get(key)
        if isinstance(value, bool):
            raise ProtocolError(f"sim_truth.{key} must be numeric")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ProtocolError(
                f"sim_truth.{key} must be numeric"
            ) from exc
        if not math.isfinite(number):
            raise ProtocolError(f"sim_truth.{key} must be finite")
        result[key] = number
    for key in ("cell", "heading"):
        if key in truth:
            result[key] = truth[key]
    return result
