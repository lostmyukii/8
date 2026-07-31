"""Newline-delimited JSON protocol helpers for RDK X3 <-> ESP32."""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping


Message = Dict[str, Any]


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

