import json

from rdk_maze_tuner.core.maze_map import Direction, MazeMap
from rdk_maze_tuner.core.maze_planner import MazePlanner
from rdk_maze_tuner.core.maze_runner import MazeRunner
from rdk_maze_tuner.core.motion_analyzer import MotionAnalyzer
from rdk_maze_tuner.core.auto_tuner import AutoTuner
from rdk_maze_tuner.core.param_manager import ParamManager
from rdk_maze_tuner.core.serial_client import SerialClient


PARAMS = "rdk_maze_tuner/config/params.yaml"
LIMITS = "rdk_maze_tuner/config/limits.yaml"


class DynamicFakeSerial:
    def __init__(self, initial_lines):
        self.read_lines = [line(item) for item in initial_lines]
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
                        "enc_left": message["target_ticks"],
                        "enc_right": message["target_ticks"],
                        "front_mm": 260,
                        "left_mm": 90,
                        "right_mm": 90,
                    }
                )
            )
        elif message["type"] == "set_params":
            self.read_lines.append(line({"type": "ack", "seq": message["seq"], "ok": True}))
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


def test_wait_telemetry_ignores_ready_and_returns_latest_sensor_frame():
    fake = DynamicFakeSerial(
        [
            {"type": "ready", "fw": "maze-esp32"},
            {"type": "telemetry", "state": "IDLE", "front_mm": 300, "left_mm": 90, "right_mm": 90},
        ]
    )
    client = SerialClient(fake, timeout_s=0.01)

    telemetry = client.wait_telemetry()

    assert telemetry["front_mm"] == 300
    assert client.last_telemetry == telemetry


def test_runner_observes_telemetry_sends_action_and_updates_position_after_done():
    fake = DynamicFakeSerial(
        [
            {"type": "telemetry", "state": "IDLE", "front_mm": 300, "left_mm": 90, "right_mm": 90},
        ]
    )
    client = SerialClient(fake, timeout_s=0.01)
    params = ParamManager(params_path=__import__("pathlib").Path(PARAMS), limits_path=__import__("pathlib").Path(LIMITS))
    maze = MazeMap(wall_threshold_mm=params.get("tof.wall_threshold_mm"))
    runner = MazeRunner(client=client, params=params, maze=maze, planner=MazePlanner())

    step = runner.run_step()

    assert step.action.name == "move_cell"
    assert step.action_id == "maze-0001"
    assert maze.position == (0, 1)
    assert maze.heading == Direction.NORTH
    assert sent_messages(fake) == [
        {
            "type": "action",
            "seq": 1,
            "action_id": "maze-0001",
            "name": "move_cell",
            "speed": 0.25,
            "target_ticks": 1350,
        }
    ]


def test_runner_returns_stop_step_without_sending_action_when_no_open_path():
    fake = DynamicFakeSerial(
        [
            {"type": "telemetry", "state": "IDLE", "front_mm": 90, "left_mm": 90, "right_mm": 90},
        ]
    )
    client = SerialClient(fake, timeout_s=0.01)
    params = ParamManager(params_path=__import__("pathlib").Path(PARAMS), limits_path=__import__("pathlib").Path(LIMITS))
    runner = MazeRunner(
        client=client,
        params=params,
        maze=MazeMap(wall_threshold_mm=params.get("tof.wall_threshold_mm")),
        planner=MazePlanner(),
    )

    step = runner.run_step()

    assert step.action.name == "stop"
    assert step.done is None
    assert sent_messages(fake) == []


def test_runner_optionally_analyzes_and_auto_tunes_after_done():
    fake = DynamicFakeSerial(
        [
            {"type": "telemetry", "state": "IDLE", "front_mm": 300, "left_mm": 90, "right_mm": 90},
        ]
    )
    client = SerialClient(fake, timeout_s=0.01)
    params = ParamManager(params_path=__import__("pathlib").Path(PARAMS), limits_path=__import__("pathlib").Path(LIMITS))
    runner = MazeRunner(
        client=client,
        params=params,
        maze=MazeMap(wall_threshold_mm=params.get("tof.wall_threshold_mm")),
        planner=MazePlanner(),
        analyzer=MotionAnalyzer(params),
        tuner=AutoTuner(params),
    )

    step = runner.run_step()

    assert step.motion_report is not None
    assert step.tune_event is not None
    assert step.tune_event["source"] == "auto_tune"
    assert params.param_version >= 1


class PauseBeforeStep:
    def pause_requested(self):
        return True

    def stop_requested(self):
        return False


class MustNotReadClient:
    def wait_telemetry(self):
        raise AssertionError("transport must not be read after pause request")


def test_runner_checks_pause_token_before_reading_transport():
    params = ParamManager(
        params_path=__import__("pathlib").Path(PARAMS),
        limits_path=__import__("pathlib").Path(LIMITS),
    )
    runner = MazeRunner(
        client=MustNotReadClient(),
        params=params,
        maze=MazeMap(wall_threshold_mm=params.get("tof.wall_threshold_mm")),
        planner=MazePlanner(),
    )

    result = runner.run_step(
        control=PauseBeforeStep(),
        goal=lambda _maze, _telemetry: False,
        event_sink=lambda _event: None,
    )

    assert result.outcome == "paused"
    assert result.action.name == "pause"


def test_runner_reports_goal_and_structured_step_events():
    fake = DynamicFakeSerial(
        [
            {
                "type": "telemetry",
                "state": "IDLE",
                "front_mm": 300,
                "left_mm": 90,
                "right_mm": 90,
            },
        ]
    )
    params = ParamManager(
        params_path=__import__("pathlib").Path(PARAMS),
        limits_path=__import__("pathlib").Path(LIMITS),
    )
    runner = MazeRunner(
        client=SerialClient(fake, timeout_s=0.01),
        params=params,
        maze=MazeMap(wall_threshold_mm=params.get("tof.wall_threshold_mm")),
        planner=MazePlanner(),
    )
    events = []

    result = runner.run_step(
        goal=lambda maze, _telemetry: maze.position == (0, 0),
        event_sink=events.append,
    )

    assert result.outcome == "goal_reached"
    assert sent_messages(fake) == []
    assert [event["type"] for event in events] == [
        "step.started",
        "telemetry",
        "maze_update",
        "step.goal_reached",
    ]


def test_runner_reports_exhausted_separately_from_goal():
    fake = DynamicFakeSerial(
        [
            {
                "type": "telemetry",
                "state": "IDLE",
                "front_mm": 90,
                "left_mm": 90,
                "right_mm": 90,
            },
        ]
    )
    params = ParamManager(
        params_path=__import__("pathlib").Path(PARAMS),
        limits_path=__import__("pathlib").Path(LIMITS),
    )
    runner = MazeRunner(
        client=SerialClient(fake, timeout_s=0.01),
        params=params,
        maze=MazeMap(wall_threshold_mm=params.get("tof.wall_threshold_mm")),
        planner=MazePlanner(),
    )

    result = runner.run_step(
        goal=lambda _maze, _telemetry: False,
        event_sink=lambda _event: None,
    )

    assert result.outcome == "exhausted"
