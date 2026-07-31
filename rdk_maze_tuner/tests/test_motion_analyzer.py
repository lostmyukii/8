from pathlib import Path

from rdk_maze_tuner.core.motion_analyzer import MotionAnalyzer
from rdk_maze_tuner.core.param_manager import ParamManager


PARAMS = Path("rdk_maze_tuner/config/params.yaml")
LIMITS = Path("rdk_maze_tuner/config/limits.yaml")


def manager():
    return ParamManager(params_path=PARAMS, limits_path=LIMITS)


def test_analyzer_detects_move_short_and_right_drift():
    analyzer = MotionAnalyzer(manager())

    report = analyzer.analyze(
        action_name="move_cell",
        target_ticks=1350,
        result={
            "type": "done",
            "action_id": "a-0001",
            "name": "move_cell",
            "success": True,
            "duration_ms": 2200,
            "enc_left": 1285,
            "enc_right": 1245,
            "front_mm": 260,
            "left_mm": 160,
            "right_mm": 280,
        },
    )

    assert report.action_id == "a-0001"
    assert report.encoder_delta == 40
    assert report.average_ticks == 1265
    assert report.distance_error_ticks == -85
    assert "move_short" in report.issues
    assert "drift_right" in report.issues


def test_analyzer_detects_turn_overshoot():
    analyzer = MotionAnalyzer(manager())

    report = analyzer.analyze(
        action_name="turn_right",
        target_ticks=720,
        result={
            "type": "done",
            "action_id": "a-0002",
            "name": "turn_right",
            "success": True,
            "duration_ms": 900,
            "enc_left": 780,
            "enc_right": -770,
        },
    )

    assert report.average_ticks == 775
    assert report.distance_error_ticks == 55
    assert report.issues == ("turn_overshoot",)


def test_analyzer_detects_obstacle_error():
    analyzer = MotionAnalyzer(manager())

    report = analyzer.analyze(
        action_name="move_cell",
        target_ticks=1350,
        result={
            "type": "error",
            "action_id": "a-0003",
            "code": "OBSTACLE_TOO_CLOSE",
            "front_mm": 55,
        },
    )

    assert report.success is False
    assert "obstacle_too_close" in report.issues

