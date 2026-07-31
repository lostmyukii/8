import json
from pathlib import Path

from rdk_maze_tuner.core.auto_tuner import AutoTuner
from rdk_maze_tuner.core.logger import JsonlLogger
from rdk_maze_tuner.core.maze_map import MazeMap, PlannedAction
from rdk_maze_tuner.core.maze_planner import MazePlanner
from rdk_maze_tuner.core.maze_runner import MazeRunner
from rdk_maze_tuner.core.motion_analyzer import MotionAnalyzer, MotionReport
from rdk_maze_tuner.core.param_manager import ParamManager
from rdk_maze_tuner.core.serial_client import SerialClient


PARAMS = Path("rdk_maze_tuner/config/params.yaml")
LIMITS = Path("rdk_maze_tuner/config/limits.yaml")


class LoggingFakeSerial:
    def __init__(self):
        self.read_lines = [
            line({"type": "telemetry", "state": "IDLE", "front_mm": 300, "left_mm": 90, "right_mm": 90})
        ]
        self.writes = []

    def write(self, data):
        self.writes.append(data)
        message = json.loads(data.decode("utf-8"))
        if message["type"] == "action":
            self.read_lines.append(line({"type": "ack", "seq": message["seq"], "ok": True}))
            self.read_lines.append(
                line(
                    {
                        "type": "done",
                        "action_id": message["action_id"],
                        "name": message["name"],
                        "success": True,
                        "duration_ms": 2200,
                        "enc_left": 1285,
                        "enc_right": 1245,
                        "front_mm": 260,
                        "left_mm": 160,
                        "right_mm": 280,
                    }
                )
            )
        return len(data)

    def flush(self):
        return None

    def readline(self):
        if not self.read_lines:
            return b""
        return self.read_lines.pop(0)


def line(message):
    return json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"


def read_jsonl(path):
    return [json.loads(item) for item in path.read_text(encoding="utf-8").splitlines()]


def test_jsonl_logger_serializes_dataclass_tuple_and_enum(tmp_path):
    path = tmp_path / "run.jsonl"
    logger = JsonlLogger(path, clock_ms=lambda: 123456)
    report = MotionReport(
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
        issues=("move_short", "drift_right"),
        confidence=0.8,
    )

    logger.record("motion_report", report)
    logger.record("planned_action", PlannedAction("move_cell"))
    logger.close()

    rows = read_jsonl(path)
    assert rows[0]["ts_ms"] == 123456
    assert rows[0]["type"] == "motion_report"
    assert rows[0]["payload"]["issues"] == ["move_short", "drift_right"]
    assert rows[1]["payload"]["name"] == "move_cell"


def test_maze_and_params_export_as_json_ready_dicts():
    params = ParamManager(params_path=PARAMS, limits_path=LIMITS)
    maze = MazeMap(wall_threshold_mm=params.get("tof.wall_threshold_mm"))
    maze.observe(front_mm=100, left_mm=300, right_mm=90)

    maze_data = maze.to_dict()
    param_data = params.snapshot()

    assert maze_data["position"] == [0, 0]
    assert maze_data["heading"] == "N"
    origin = next(cell for cell in maze_data["cells"] if cell["coord"] == [0, 0])
    assert origin["walls"]["N"] is True
    assert param_data["param_version"] == 1
    assert param_data["params"]["motor"]["base_speed"] == 0.25


def test_runner_records_experiment_events_to_jsonl(tmp_path):
    params = ParamManager(params_path=PARAMS, limits_path=LIMITS)
    log_path = tmp_path / "experiment.jsonl"
    logger = JsonlLogger(log_path, clock_ms=lambda: 42)
    runner = MazeRunner(
        client=SerialClient(LoggingFakeSerial(), timeout_s=0.01),
        params=params,
        maze=MazeMap(wall_threshold_mm=params.get("tof.wall_threshold_mm")),
        planner=MazePlanner(),
        analyzer=MotionAnalyzer(params),
        tuner=AutoTuner(params),
        logger=logger,
    )

    runner.run_step()
    logger.close()

    rows = read_jsonl(log_path)
    assert [row["type"] for row in rows] == [
        "telemetry",
        "planned_action",
        "done",
        "motion_report",
        "param_change",
        "maze_update",
    ]
    assert rows[3]["payload"]["issues"] == ["drift_right", "move_short"]
    assert rows[4]["payload"]["changes"]["motion.cell_ticks"] == [1350, 1390]
    assert rows[5]["payload"]["position"] == [0, 1]
