import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from rdk_maze_tuner.platform.database import Database
from rdk_maze_tuner.platform.event_store import EventConflictError, EventStore


FIXED_UTC = datetime(2026, 8, 1, 12, 30, 45, 123456, tzinfo=UTC)


def create_run(database: Database, run_id: str = "run-0001") -> None:
    with database.connection() as connection:
        connection.execute(
            """
            INSERT INTO runs (id, mode, status, created_at_utc)
            VALUES (?, ?, ?, ?)
            """,
            (run_id, "simulation", "RUNNING", "2026-08-01T12:00:00Z"),
        )


def make_store(tmp_path):
    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()
    create_run(database)
    store = EventStore(
        database=database,
        runs_dir=tmp_path / "runs",
        monotonic_ns=lambda: 987_654_321,
        utc_now=lambda: FIXED_UTC,
        event_id_factory=lambda: "event-generated",
    )
    return database, store


def read_jsonl(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def test_event_store_writes_sqlite_index_and_run_jsonl(tmp_path):
    database, store = make_store(tmp_path)

    event = store.append(
        run_id="run-0001",
        event_type="telemetry",
        source="simulation",
        payload={"front_mm": 320, "label": "前方"},
    )

    expected = {
        "event_id": "event-generated",
        "run_id": "run-0001",
        "monotonic_ns": 987_654_321,
        "utc_timestamp": "2026-08-01T12:30:45.123456Z",
        "type": "telemetry",
        "source": "simulation",
        "payload": {"front_mm": 320, "label": "前方"},
        "schema_version": 1,
    }
    assert event == expected

    jsonl_path = tmp_path / "runs" / "run-0001" / "events.jsonl"
    assert read_jsonl(jsonl_path) == [expected]
    assert jsonl_path.read_bytes().endswith(b"\n")

    with database.connection() as connection:
        row = connection.execute(
            """
            SELECT event_id, run_id, monotonic_ns, utc_timestamp, event_type,
                   source, payload_json, schema_version, jsonl_written
            FROM events
            """
        ).fetchone()

    assert dict(row) == {
        "event_id": "event-generated",
        "run_id": "run-0001",
        "monotonic_ns": 987_654_321,
        "utc_timestamp": "2026-08-01T12:30:45.123456Z",
        "event_type": "telemetry",
        "source": "simulation",
        "payload_json": '{"front_mm":320,"label":"前方"}',
        "schema_version": 1,
        "jsonl_written": 1,
    }


def test_duplicate_event_id_is_idempotent_across_store_instances(tmp_path):
    database, store = make_store(tmp_path)
    first = store.append(
        event_id="event-0001",
        run_id="run-0001",
        event_type="done",
        source="esp32",
        payload={"action_id": "a-0001", "success": True},
    )
    second_store = EventStore(database=database, runs_dir=tmp_path / "runs")

    second = second_store.append(
        event_id="event-0001",
        run_id="run-0001",
        event_type="done",
        source="esp32",
        payload={"action_id": "a-0001", "success": True},
    )

    assert second == first
    assert len(read_jsonl(tmp_path / "runs" / "run-0001" / "events.jsonl")) == 1
    with database.connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_id = ?",
            ("event-0001",),
        ).fetchone()[0]
    assert count == 1


def test_concurrent_duplicate_event_writes_one_jsonl_record(tmp_path, monkeypatch):
    database, first_store = make_store(tmp_path)
    second_store = EventStore(database=database, runs_dir=tmp_path / "runs")
    first_reached_file = threading.Event()
    second_finished_file = threading.Event()
    first_write = first_store._write_jsonl
    second_write = second_store._write_jsonl

    def delayed_first(event, *, scan_first):
        assert scan_first is False
        first_reached_file.set()
        assert second_finished_file.wait(timeout=2)
        first_write(event, scan_first=scan_first)

    def prioritized_second(event, *, scan_first):
        assert scan_first is True
        assert first_reached_file.wait(timeout=2)
        second_write(event, scan_first=scan_first)
        second_finished_file.set()

    monkeypatch.setattr(first_store, "_write_jsonl", delayed_first)
    monkeypatch.setattr(second_store, "_write_jsonl", prioritized_second)
    event = {
        "event_id": "event-race",
        "run_id": "run-0001",
        "event_type": "done",
        "source": "esp32",
        "payload": {"action_id": "a-0001", "success": True},
    }

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first_store.append, **event)
        assert first_reached_file.wait(timeout=2)
        second_future = executor.submit(second_store.append, **event)
        assert second_future.result(timeout=2) == first_future.result(timeout=2)

    rows = read_jsonl(tmp_path / "runs" / "run-0001" / "events.jsonl")
    assert [row["event_id"] for row in rows] == ["event-race"]


def test_duplicate_event_id_with_different_content_is_rejected(tmp_path):
    _, store = make_store(tmp_path)
    store.append(
        event_id="event-0001",
        run_id="run-0001",
        event_type="telemetry",
        source="esp32",
        payload={"front_mm": 300},
    )

    with pytest.raises(EventConflictError, match="event-0001"):
        store.append(
            event_id="event-0001",
            run_id="run-0001",
            event_type="telemetry",
            source="esp32",
            payload={"front_mm": 55},
        )

    assert len(read_jsonl(tmp_path / "runs" / "run-0001" / "events.jsonl")) == 1


def test_missing_run_rolls_back_without_creating_jsonl(tmp_path):
    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()
    store = EventStore(database=database, runs_dir=tmp_path / "runs")

    with pytest.raises(sqlite3.IntegrityError):
        store.append(
            event_id="event-0001",
            run_id="missing-run",
            event_type="telemetry",
            source="esp32",
            payload={},
        )

    assert not (tmp_path / "runs" / "missing-run" / "events.jsonl").exists()
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_run_id_cannot_escape_runs_directory(tmp_path):
    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()
    store = EventStore(database=database, runs_dir=tmp_path / "runs")

    with pytest.raises(ValueError, match="run_id"):
        store.append(
            event_id="event-0001",
            run_id="../outside",
            event_type="telemetry",
            source="esp32",
            payload={},
        )

    assert not (tmp_path / "outside").exists()
