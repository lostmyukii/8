from rdk_maze_tuner.core.protocol import (
    ProtocolError,
    build_action,
    build_heartbeat,
    build_set_params,
    decode_line,
    encode_message,
)


def test_encode_message_appends_newline_and_keeps_compact_json():
    assert encode_message({"type": "heartbeat", "seq": 1}) == b'{"type":"heartbeat","seq":1}\n'


def test_decode_line_rejects_non_object_payload():
    try:
        decode_line(b"[1,2]\n")
    except ProtocolError as exc:
        assert "object" in str(exc)
    else:
        raise AssertionError("expected ProtocolError")


def test_decode_line_rejects_invalid_json():
    try:
        decode_line(b"{bad json}\n")
    except ProtocolError as exc:
        assert "invalid JSON" in str(exc)
    else:
        raise AssertionError("expected ProtocolError")


def test_builders_create_documented_messages():
    assert build_heartbeat(seq=7, ts_ms=123) == {"type": "heartbeat", "seq": 7, "ts_ms": 123}
    assert build_set_params(seq=8, params={"base_speed": 0.25}) == {
        "type": "set_params",
        "seq": 8,
        "params": {"base_speed": 0.25},
    }
    assert build_action(seq=9, action_id="a-0001", name="move_cell", speed=0.25, target_ticks=1350) == {
        "type": "action",
        "seq": 9,
        "action_id": "a-0001",
        "name": "move_cell",
        "speed": 0.25,
        "target_ticks": 1350,
    }

