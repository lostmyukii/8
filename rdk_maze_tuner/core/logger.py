"""JSONL experiment logging utilities."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable


class JsonlLogger:
    def __init__(self, path: Path, *, clock_ms: Callable[[], int] | None = None) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._handle = self.path.open("a", encoding="utf-8")

    def record(self, event_type: str, payload: Any) -> dict:
        row = {
            "ts_ms": self.clock_ms(),
            "type": event_type,
            "payload": _json_ready(payload),
        }
        self._handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._handle.flush()
        return row

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()

    def __enter__(self) -> "JsonlLogger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _json_ready(value: Any) -> Any:
    if is_dataclass(value):
        return _json_ready(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(item) for item in value]
    return value


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_json_ready(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
