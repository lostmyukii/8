import json
import threading
import time
from collections import deque

import pytest

from rdk_maze_tuner.core.device_session import (
    DeviceDisconnectedError,
    DeviceSession,
    DeviceSessionError,
)
from rdk_maze_tuner.core.serial_client import SerialClient, SerialClientError


def line(message):
    return json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"


class InteractiveSerial:
    def __init__(self, *, auto_reply=True):
        self._condition = threading.Condition()
        self._read_lines = deque()
        self._closed = False
        self.writes = []
        self.active_readers = 0
        self.max_active_readers = 0
        self.pending_action = None
        self.auto_reply = auto_reply

    def write(self, data):
        message = json.loads(data.decode("utf-8"))
        self.writes.append(message)
        if message["type"] == "action":
            self.pending_action = message
            if self.auto_reply:
                self.feed({"type": "ack", "seq": message["seq"], "ok": True})
                self.feed(
                    {
                        "type": "telemetry",
                        "state": "MOVING_CELL",
                        "front_mm": 260,
                    }
                )
        elif self.auto_reply:
            self.feed({"type": "ack", "seq": message["seq"], "ok": True})
        return len(data)

    def flush(self):
        return None

    def readline(self):
        with self._condition:
            self.active_readers += 1
            self.max_active_readers = max(
                self.max_active_readers,
                self.active_readers,
            )
            try:
                self._condition.wait_for(
                    lambda: self._read_lines or self._closed,
                    timeout=0.01,
                )
                if self._read_lines:
                    item = self._read_lines.popleft()
                    if isinstance(item, BaseException):
                        raise item
                    return item
                if self._closed:
                    raise OSError("device disconnected")
                return b""
            finally:
                self.active_readers -= 1

    def feed(self, message):
        with self._condition:
            self._read_lines.append(line(message))
            self._condition.notify_all()

    def complete_action(self):
        assert self.pending_action is not None
        self.feed(
            {
                "type": "done",
                "action_id": self.pending_action["action_id"],
                "name": self.pending_action["name"],
                "success": True,
            }
        )

    def disconnect(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()


def test_single_background_reader_routes_ack_result_and_telemetry():
    stream = InteractiveSerial()
    client = SerialClient(stream, timeout_s=0.5)
    session = DeviceSession(client)
    subscription = session.subscribe(message_types={"telemetry"})
    session.start()

    result = {}

    def run_action():
        result["value"] = session.execute_action_with_ack(
            action_id="a-0001",
            name="move_cell",
            speed=0.25,
            target_ticks=1350,
        )

    action_thread = threading.Thread(target=run_action)
    action_thread.start()
    telemetry = subscription.get(timeout_s=0.5)
    stream.complete_action()
    action_thread.join(timeout=1.0)

    try:
        ack, done = result["value"]
        assert ack == {"type": "ack", "seq": 1, "ok": True}
        assert done["action_id"] == "a-0001"
        assert telemetry["type"] == "telemetry"
        assert session.last_telemetry == telemetry
        assert stream.max_active_readers == 1
        with pytest.raises(SerialClientError, match="owned by DeviceSession"):
            client.read_message()
    finally:
        subscription.close()
        session.close()


def test_action_result_can_outlive_short_command_ack_timeout():
    stream = InteractiveSerial()
    session = DeviceSession(
        SerialClient(stream, timeout_s=0.02),
        action_result_timeout_s=0.2,
    )
    session.start()
    result = {}

    def run_action():
        result["value"] = session.execute_action_with_ack(
            action_id="a-slow-cell",
            name="move_cell",
            speed=0.25,
            target_ticks=2430,
        )

    action_thread = threading.Thread(target=run_action)
    action_thread.start()
    deadline = time.monotonic() + 0.1
    while stream.pending_action is None and time.monotonic() < deadline:
        time.sleep(0.001)

    time.sleep(0.04)
    assert action_thread.is_alive()
    stream.complete_action()
    action_thread.join(timeout=0.5)

    try:
        assert result["value"][1]["action_id"] == "a-slow-cell"
    finally:
        session.close()


def test_concurrent_heartbeat_and_action_do_not_steal_each_others_messages():
    stream = InteractiveSerial()
    session = DeviceSession(SerialClient(stream, timeout_s=0.5))
    session.start()
    action_result = {}

    def run_action():
        action_result["value"] = session.execute_action(
            action_id="a-concurrent",
            name="move_cell",
            speed=0.25,
            target_ticks=1350,
        )

    action_thread = threading.Thread(target=run_action)
    action_thread.start()
    deadline = time.monotonic() + 0.5
    while stream.pending_action is None and time.monotonic() < deadline:
        time.sleep(0.001)

    heartbeat_ack = session.send_heartbeat(ts_ms=123456)
    stream.complete_action()
    action_thread.join(timeout=1.0)

    try:
        assert heartbeat_ack == {"type": "ack", "seq": 2, "ok": True}
        assert action_result["value"]["action_id"] == "a-concurrent"
        assert stream.max_active_readers == 1
    finally:
        session.close()


def test_disconnect_fails_all_pending_waiters_with_explicit_error():
    stream = InteractiveSerial(auto_reply=False)
    session = DeviceSession(SerialClient(stream, timeout_s=1.0))
    session.start()
    errors = []

    def wait_for_action():
        try:
            session.execute_action(
                action_id="a-first",
                name="move_cell",
                speed=0.25,
                target_ticks=1350,
            )
        except BaseException as exc:
            errors.append(exc)

    def wait_for_heartbeat():
        try:
            session.send_heartbeat(ts_ms=123456)
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=wait_for_action)
    first.start()
    deadline = time.monotonic() + 0.5
    while stream.pending_action is None and time.monotonic() < deadline:
        time.sleep(0.001)
    second = threading.Thread(target=wait_for_heartbeat)
    second.start()
    deadline = time.monotonic() + 0.5
    while len(stream.writes) < 2 and time.monotonic() < deadline:
        time.sleep(0.001)
    stream.disconnect()
    first.join(timeout=1.0)
    second.join(timeout=1.0)

    try:
        assert len(errors) == 2
        assert all(
            isinstance(error, DeviceDisconnectedError) for error in errors
        )
        assert all("disconnected" in str(error).lower() for error in errors)
        assert session.connected is False
    finally:
        session.close()


def test_cached_ready_is_not_reused_after_disconnect():
    stream = InteractiveSerial()
    session = DeviceSession(SerialClient(stream, timeout_s=0.5))
    session.start()
    stream.feed({"type": "ready", "fw": "maze-esp32"})
    assert session.wait_ready(timeout_s=0.5)["fw"] == "maze-esp32"
    stream.disconnect()
    deadline = time.monotonic() + 0.5
    while session.connected and time.monotonic() < deadline:
        time.sleep(0.001)

    try:
        with pytest.raises(DeviceDisconnectedError):
            session.wait_ready(timeout_s=0.01)
    finally:
        session.close()


def test_recovery_fields_are_written_and_completed_action_id_cannot_be_reused():
    stream = InteractiveSerial()
    session = DeviceSession(SerialClient(stream, timeout_s=0.5))
    session.start()
    result = {}

    def run_action():
        result["value"] = session.execute_action(
            action_id="a-0001-recovery-1",
            name="align_heading",
            speed=0.09,
            target_ticks=60,
            recovery=True,
            direction="left",
            parent_action_id="a-0001",
        )

    action_thread = threading.Thread(target=run_action)
    action_thread.start()
    deadline = time.monotonic() + 0.5
    while stream.pending_action is None and time.monotonic() < deadline:
        time.sleep(0.001)
    assert stream.pending_action["recovery"] is True
    assert stream.pending_action["direction"] == "left"
    assert stream.pending_action["parent_action_id"] == "a-0001"
    stream.complete_action()
    action_thread.join(timeout=1.0)

    try:
        assert result["value"]["action_id"] == "a-0001-recovery-1"
        writes_before_reuse = len(stream.writes)
        with pytest.raises(DeviceSessionError, match="already used"):
            session.execute_action(
                action_id="a-0001-recovery-1",
                name="align_heading",
                speed=0.09,
                target_ticks=60,
                recovery=True,
                direction="left",
                parent_action_id="a-0001",
            )
        assert len(stream.writes) == writes_before_reuse
    finally:
        session.close()
