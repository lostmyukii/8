from rdk_maze_tuner.core.protocol import (
    ProtocolError,
    SIMULATION_TRUTH_FIELDS,
    build_action,
    build_heartbeat,
    build_set_params,
    decode_line,
    encode_message,
    extract_simulation_truth,
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


def test_recovery_action_keeps_explicit_parent_and_direction_fields():
    assert build_action(
        seq=10,
        action_id="a-0001-recovery-1",
        name="align_heading",
        speed=0.09,
        target_ticks=60,
        recovery=True,
        direction="left",
        parent_action_id="a-0001",
    ) == {
        "type": "action",
        "seq": 10,
        "action_id": "a-0001-recovery-1",
        "name": "align_heading",
        "speed": 0.09,
        "target_ticks": 60,
        "recovery": True,
        "direction": "left",
        "parent_action_id": "a-0001",
    }


def test_simulation_truth_is_strictly_evaluation_only():
    truth = extract_simulation_truth(
        {
            "sim_truth": {
                "x_mm": 100,
                "y_mm": 200,
                "yaw_deg": 30,
                "linear_speed_mm_s": 50,
                "angular_velocity_dps": 5,
                "body_longitudinal_speed_mm_s": 45,
                "left_slip_rate": 0.1,
                "right_slip_rate": 0.2,
                "active_surface": "normal",
                "collision_count": 0,
                "cell": [0, 0],
            }
        }
    )

    assert frozenset(truth) == SIMULATION_TRUTH_FIELDS
    assert "cell" not in truth


def test_simulation_truth_keeps_signed_formula_slip_but_not_fusion_access():
    message = {
        "enc_left": 10,
        "enc_right": 10,
        "sim_truth": {
            "x_mm": 0,
            "y_mm": 0,
            "yaw_deg": 0,
            "body_longitudinal_speed_mm_s": -20,
            "left_slip_rate": -0.75,
            "right_slip_rate": 1.25,
        },
    }

    truth = extract_simulation_truth(message)
    fusion = __import__(
        "rdk_maze_tuner.core.protocol",
        fromlist=["extract_fusion_telemetry"],
    ).extract_fusion_telemetry(message)

    assert truth["left_slip_rate"] == -0.75
    assert truth["right_slip_rate"] == 1.25
    assert truth["body_longitudinal_speed_mm_s"] == -20
    assert "sim_truth" not in fusion
