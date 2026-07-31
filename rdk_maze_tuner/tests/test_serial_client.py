import json

import pytest

from rdk_maze_tuner.core.serial_client import SerialClient, SerialClientError, TimeoutError


class FakeSerial:
    def __init__(self, read_lines):
        self.read_lines = list(read_lines)
        self.writes = []

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def flush(self):
        return None

    def readline(self):
        if not self.read_lines:
            return b""
        return self.read_lines.pop(0)


def line(message):
    return json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"


def sent_messages(fake):
    return [json.loads(item.decode("utf-8")) for item in fake.writes]


def test_wait_ready_returns_ready_message():
    fake = FakeSerial([line({"type": "ready", "fw": "maze-esp32", "version": "0.1.0"})])
    client = SerialClient(fake, timeout_s=0.01)

    ready = client.wait_ready()

    assert ready["type"] == "ready"
    assert ready["fw"] == "maze-esp32"


def test_send_params_writes_set_params_and_requires_matching_ack():
    fake = FakeSerial(
        [
            line({"type": "telemetry", "state": "IDLE", "front_mm": 300}),
            line({"type": "ack", "seq": 1, "ok": True}),
        ]
    )
    client = SerialClient(fake, timeout_s=0.01)

    ack = client.send_params({"base_speed": 0.25, "cell_ticks": 1350})

    assert ack["ok"] is True
    assert sent_messages(fake) == [
        {"type": "set_params", "seq": 1, "params": {"base_speed": 0.25, "cell_ticks": 1350}}
    ]


def test_execute_action_waits_for_matching_done_after_ack_and_telemetry():
    fake = FakeSerial(
        [
            line({"type": "ack", "seq": 1, "ok": True}),
            line({"type": "telemetry", "state": "MOVING_CELL", "front_mm": 260}),
            line({"type": "done", "action_id": "old", "success": True}),
            line({"type": "done", "action_id": "a-0001", "success": True, "enc_left": 1352, "enc_right": 1347}),
        ]
    )
    client = SerialClient(fake, timeout_s=0.01)

    done = client.execute_action(action_id="a-0001", name="move_cell", speed=0.25, target_ticks=1350)

    assert done["type"] == "done"
    assert done["action_id"] == "a-0001"
    assert done["enc_left"] == 1352
    assert sent_messages(fake) == [
        {
            "type": "action",
            "seq": 1,
            "action_id": "a-0001",
            "name": "move_cell",
            "speed": 0.25,
            "target_ticks": 1350,
        }
    ]


def test_execute_action_raises_when_matching_error_arrives():
    fake = FakeSerial(
        [
            line({"type": "ack", "seq": 1, "ok": True}),
            line({"type": "error", "action_id": "a-0002", "code": "OBSTACLE_TOO_CLOSE", "front_mm": 55}),
        ]
    )
    client = SerialClient(fake, timeout_s=0.01)

    with pytest.raises(SerialClientError) as exc:
        client.execute_action(action_id="a-0002", name="move_cell", speed=0.25, target_ticks=1350)

    assert "OBSTACLE_TOO_CLOSE" in str(exc.value)


def test_wait_ready_times_out_when_no_lines_arrive():
    client = SerialClient(FakeSerial([]), timeout_s=0.0)

    with pytest.raises(TimeoutError):
        client.wait_ready()

