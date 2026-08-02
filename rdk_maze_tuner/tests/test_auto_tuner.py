from pathlib import Path

import pytest

from rdk_maze_tuner.core.auto_tuner import AutoTuner
from rdk_maze_tuner.core.motion_analyzer import MotionReport
from rdk_maze_tuner.core.param_manager import ParamManager


PARAMS = Path("rdk_maze_tuner/config/params.yaml")
LIMITS = Path("rdk_maze_tuner/config/limits.yaml")


def manager():
    return ParamManager(params_path=PARAMS, limits_path=LIMITS)


def report(*issues):
    return MotionReport(
        action_id="a-0001",
        name="move_cell",
        success=True,
        target_ticks=1350,
        average_ticks=1265,
        distance_error_ticks=-85,
        encoder_delta=40,
        left_right_ratio=1.03,
        duration_ms=2200,
        front_mm=260,
        left_mm=160,
        right_mm=280,
        issues=tuple(issues),
        confidence=0.8,
    )


def test_auto_tuner_applies_limited_move_and_drift_updates():
    params = manager()
    event = AutoTuner(params).apply(report("move_short", "drift_right"))

    assert event["type"] == "param_change"
    assert event["source"] == "auto_tune"
    assert event["reason"] == "move_short,drift_right"
    assert event["changes"]["motion.cell_ticks"] == [1350, 1390]
    assert event["changes"]["motor.left_trim"] == [1.0, 0.98]
    assert event["changes"]["motor.right_trim"] == [1.0, 1.01]
    assert "safety.heartbeat_timeout_ms" not in event["changes"]


def test_auto_tuner_respects_max_params_per_step():
    params = manager()
    event = AutoTuner(params).apply(report("move_short", "drift_right", "obstacle_too_close"))

    assert len(event["changes"]) == 3
    assert "tof.front_stop_mm" not in event["changes"]


def test_auto_tuner_clamps_to_limits():
    params = manager()
    params.apply_updates({"tof.front_stop_mm": 245}, source="test")

    event = AutoTuner(params).apply(report("obstacle_too_close"))

    assert event["changes"]["tof.front_stop_mm"] == [245, 250]


def test_auto_tuner_filters_arrival_verification_even_if_rule_proposes_it(
    monkeypatch,
):
    params = manager()
    tuner = AutoTuner(params)
    monkeypatch.setattr(
        tuner,
        "_updates_for_issue",
        lambda _issue: {
            "arrival_verification.goal_min_confidence": 0.50,
            "motor.base_speed": 0.20,
        },
    )

    updates = tuner.propose(report("synthetic"))

    assert updates == {"motor.base_speed": 0.20}
    assert all(
        not path.startswith("arrival_verification.")
        for path in updates
    )


def test_auto_tuner_direct_arrival_update_is_rejected():
    params = manager()

    with pytest.raises(Exception, match="cannot modify"):
        params.apply_updates(
            {"arrival_verification.goal_min_confidence": 0.50},
            source="auto_tune",
        )
