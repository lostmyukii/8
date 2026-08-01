"""JSONL experiment logging utilities."""

from __future__ import annotations

import json
import os
import tempfile
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
    write_json_atomic(path, payload)


def write_json_atomic(path: Path, payload: Any) -> None:
    """Atomically replace a JSON document and fsync its final contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(
            _json_ready(payload),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(
                    f"{path}:{line_number} must contain a JSON object"
                )
            rows.append(value)
    return rows
