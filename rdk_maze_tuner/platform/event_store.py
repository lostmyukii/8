"""Recoverable SQLite index plus append-only JSONL event storage."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator

import fcntl

from .database import Database


SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class EventConflictError(RuntimeError):
    """Raised when an event ID is reused for different immutable content."""


class EventLogCorruptionError(RuntimeError):
    """Raised when an existing JSONL record cannot be trusted."""


class EventStore:
    def __init__(
        self,
        *,
        database: Database,
        runs_dir: Path,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        utc_now: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.database = database
        self.runs_dir = Path(runs_dir)
        self.monotonic_ns = monotonic_ns
        self.utc_now = utc_now or (lambda: datetime.now(UTC))
        self.event_id_factory = event_id_factory or (lambda: str(uuid.uuid4()))

    def append(
        self,
        *,
        run_id: str,
        event_type: str,
        source: str,
        payload: Any,
        event_id: str | None = None,
        schema_version: int = 1,
    ) -> dict[str, Any]:
        _validate_run_id(run_id)
        event_id = str(event_id or self.event_id_factory())
        if not event_id:
            raise ValueError("event_id must not be empty")
        if not event_type:
            raise ValueError("event_type must not be empty")
        if not source:
            raise ValueError("source must not be empty")
        if not isinstance(schema_version, int) or schema_version <= 0:
            raise ValueError("schema_version must be a positive integer")

        payload_json = _canonical_json(payload)
        proposed = {
            "event_id": event_id,
            "run_id": run_id,
            "monotonic_ns": int(self.monotonic_ns()),
            "utc_timestamp": _utc_text(self.utc_now()),
            "type": str(event_type),
            "source": str(source),
            "payload": json.loads(payload_json),
            "schema_version": schema_version,
        }

        stored, created_new, jsonl_written = self._insert_or_get(
            proposed,
            payload_json=payload_json,
        )
        if not jsonl_written:
            self._write_jsonl(stored, scan_first=not created_new)
        return stored

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        _validate_run_id(run_id)
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT event_id, run_id, monotonic_ns, utc_timestamp, event_type,
                       source, payload_json, schema_version
                FROM events
                WHERE run_id = ?
                ORDER BY monotonic_ns, event_id
                """,
                (run_id,),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def _insert_or_get(
        self,
        proposed: dict[str, Any],
        *,
        payload_json: str,
    ) -> tuple[dict[str, Any], bool, bool]:
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT event_id, run_id, monotonic_ns, utc_timestamp, event_type,
                       source, payload_json, schema_version, jsonl_written
                FROM events
                WHERE event_id = ?
                """,
                (proposed["event_id"],),
            ).fetchone()
            if row is not None:
                stored = _event_from_row(row)
                _assert_same_event(stored, proposed)
                return stored, False, bool(row["jsonl_written"])

            connection.execute(
                """
                INSERT INTO events (
                    event_id, run_id, monotonic_ns, utc_timestamp, event_type,
                    source, payload_json, schema_version, jsonl_written, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    proposed["event_id"],
                    proposed["run_id"],
                    proposed["monotonic_ns"],
                    proposed["utc_timestamp"],
                    proposed["type"],
                    proposed["source"],
                    payload_json,
                    proposed["schema_version"],
                    _utc_text(datetime.now(UTC)),
                ),
            )
        return proposed, True, False

    def _write_jsonl(
        self,
        event: dict[str, Any],
        *,
        scan_first: bool,
    ) -> None:
        run_dir = self.runs_dir / event["run_id"]
        run_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = run_dir / "events.jsonl"
        lock_path = run_dir / ".events.lock"
        encoded = _canonical_json(event)

        with _exclusive_lock(lock_path):
            with self.database.connection() as connection:
                row = connection.execute(
                    "SELECT jsonl_written FROM events WHERE event_id = ?",
                    (event["event_id"],),
                ).fetchone()
            if row is None:
                raise RuntimeError(
                    f"event index disappeared before JSONL sync: {event['event_id']}"
                )
            if row["jsonl_written"]:
                return
            if scan_first and _existing_event(jsonl_path, event) is not None:
                pass
            else:
                with jsonl_path.open("a", encoding="utf-8") as handle:
                    handle.write(encoded + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            with self.database.connection() as connection:
                connection.execute(
                    "UPDATE events SET jsonl_written = 1 WHERE event_id = ?",
                    (event["event_id"],),
                )


def _validate_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or SAFE_RUN_ID.fullmatch(run_id) is None:
        raise ValueError("run_id contains unsafe characters")


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"value is not valid JSON: {exc}") from exc


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC clock must return a timezone-aware datetime")
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _event_from_row(row: Any) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "run_id": row["run_id"],
        "monotonic_ns": row["monotonic_ns"],
        "utc_timestamp": row["utc_timestamp"],
        "type": row["event_type"],
        "source": row["source"],
        "payload": json.loads(row["payload_json"]),
        "schema_version": row["schema_version"],
    }


def _assert_same_event(
    stored: dict[str, Any],
    proposed: dict[str, Any],
) -> None:
    immutable_fields = (
        "event_id",
        "run_id",
        "type",
        "source",
        "payload",
        "schema_version",
    )
    if any(stored[field] != proposed[field] for field in immutable_fields):
        raise EventConflictError(
            f"event_id {stored['event_id']!r} already has different content"
        )


def _existing_event(
    jsonl_path: Path,
    expected: dict[str, Any],
) -> dict[str, Any] | None:
    if not jsonl_path.exists():
        return None
    with jsonl_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EventLogCorruptionError(
                    f"invalid JSONL at {jsonl_path}:{line_number}"
                ) from exc
            if event.get("event_id") != expected["event_id"]:
                continue
            if event != expected:
                raise EventConflictError(
                    f"event_id {expected['event_id']!r} conflicts with JSONL"
                )
            return event
    return None


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
