import json
from types import SimpleNamespace

from rdk_maze_tuner.core.maze_definition import (
    MapDefinition,
    StartPose,
    WallSegment,
)
from rdk_maze_tuner.core.maze_map import Direction, MazeMap, PlannedAction
from rdk_maze_tuner.core.maze_planner import MazePlanner
from rdk_maze_tuner.core.maze_runner import MazeRunner
from rdk_maze_tuner.core.motion_evidence import (
    ArrivalVerificationConfig,
    MotionEvidenceDecision,
    RecoverySuggestion,
)
from rdk_maze_tuner.core.motion_analyzer import MotionAnalyzer
from rdk_maze_tuner.core.motion_targets import MotionTargetResolver
from rdk_maze_tuner.core.auto_tuner import AutoTuner
from rdk_maze_tuner.core.param_manager import ParamManager
from rdk_maze_tuner.core.serial_client import SerialClient
from rdk_maze_tuner.core.task_pose_tracker import TaskPoseTracker


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


def corridor_definition():
    rows = 2
    cols = 1
    walls = (
        WallSegment(0, 0, 1, 0),
        WallSegment(0, 2, 1, 2),
        WallSegment(0, 0, 0, 1),
        WallSegment(0, 1, 0, 2),
        WallSegment(1, 0, 1, 1),
        WallSegment(1, 1, 1, 2),
    )
    return MapDefinition(
        rows=rows,
        cols=cols,
        cell_width_mm=450,
        cell_height_mm=450,
        wall_thickness_mm=40,
        wall_height_mm=180,
        start=StartPose(0, 1, "N"),
        goals=((0, 0),),
        walls=walls,
    )


class FixedPlanner:
    def __init__(self, action):
        self.action = action

    def next_action(self, _maze):
        return self.action


class EvidenceClient:
    def __init__(self, baseline, result):
        self.baseline = dict(baseline)
        self.result = dict(result)
        self.executed = []

    def wait_telemetry(self):
        return dict(self.baseline)

    def execute_action(self, **command):
        self.executed.append(command)
        return {**self.result, "action_id": self.result.get("action_id", command["action_id"])}


class TelemetrySubscription:
    def __init__(self, messages):
        self.messages = list(messages)
        self.closed = False

    def get(self, *, timeout_s=0.0):
        if not self.messages:
            return None
        return self.messages.pop(0)

    def close(self):
        self.closed = True


class SubscribedEvidenceClient(EvidenceClient):
    def __init__(self, baseline, result, completion_telemetry):
        super().__init__(baseline, result)
        self.subscription = TelemetrySubscription(
            [completion_telemetry]
        )

    def subscribe(self, *, message_types, max_queue):
        assert set(message_types) == {"telemetry"}
        return self.subscription


class RecoveryClient:
    def __init__(self):
        self.executed = []
        self._encoders = 0

    def wait_telemetry(self):
        return {
            "type": "telemetry",
            "ts_ms": 0,
            "enc_left": 0,
            "enc_right": 0,
            "front_mm": 500,
            "left_mm": 100,
            "right_mm": 100,
            "imu_available": False,
        }

    def execute_action(self, **command):
        self.executed.append(command)
        self._encoders += command["target_ticks"]
        return {
            "type": "done",
            "action_id": command["action_id"],
            "name": command["name"],
            "success": True,
            "duration_ms": 100,
            "enc_left": self._encoders,
            "enc_right": self._encoders,
        }


class ScriptedRecoveryTracker:
    def __init__(self, recovery_statuses):
        self.recovery_statuses = list(recovery_statuses)
        self.recovery_calls = []
        self.commit_count = 0
        self.gate = SimpleNamespace(
            config=ArrivalVerificationConfig(
                max_recovery_attempts_per_cell=2
            )
        )
        self._pose = SimpleNamespace(
            to_dict=lambda: {
                "cell": [0, 0],
                "heading": "N",
                "confidence": 0.95,
            }
        )

    def estimate(self):
        return self._pose

    def check_map_conflict(self, _telemetry):
        return None

    def begin_action(self, **_kwargs):
        return {"saved": True}

    def complete_action(self, **_kwargs):
        return self._tracked("recoverable")

    def complete_recovery(self, **kwargs):
        self.recovery_calls.append(kwargs)
        return self._tracked(self.recovery_statuses.pop(0))

    def accept_action(self, _action):
        self.commit_count += 1
        return self._pose

    def _tracked(self, status):
        recovery = (
            RecoverySuggestion(
                kind="nudge_forward",
                remaining_distance_mm=40.0,
                max_distance_mm=62.5,
            )
            if status == "recoverable"
            else None
        )
        return SimpleNamespace(
            decision=MotionEvidenceDecision(
                status=status,
                code=None,
                position_error_ratio=0.12,
                heading_error_deg=2.0,
                pose_confidence=0.95,
                recovery=recovery,
            ),
            pose=self._pose,
        )


def evidence_runner(*, result, conflict_required_samples=3):
    params = ParamManager(
        params_path=__import__("pathlib").Path(PARAMS),
        limits_path=__import__("pathlib").Path(LIMITS),
    )
    maze = MazeMap.from_definition(
        corridor_definition(),
        wall_threshold_mm=150,
        map_version_id="corridor-v1",
    )
    baseline = {
        "type": "telemetry",
        "ts_ms": 0,
        "enc_left": 0,
        "enc_right": 0,
        "front_mm": 675,
        "left_mm": 225,
        "right_mm": 225,
        "imu_available": False,
    }
    client = EvidenceClient(baseline, result)
    pose_tracker = TaskPoseTracker.from_params(
        maze=maze,
        params=params,
        arrival_config=ArrivalVerificationConfig(),
        run_id="run-evidence",
        conflict_required_samples=conflict_required_samples,
    )
    runner = MazeRunner(
        client=client,
        params=params,
        maze=maze,
        planner=FixedPlanner(PlannedAction("move_cell", Direction.NORTH)),
        pose_tracker=pose_tracker,
    )
    return runner, client, maze


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


def test_runner_records_map_distance_ticks_per_mm_and_resolved_target():
    fake = DynamicFakeSerial(
        [
            {
                "type": "telemetry",
                "state": "IDLE",
                "front_mm": 500,
                "left_mm": 90,
                "right_mm": 90,
            },
        ]
    )
    params = ParamManager(
        params_path=__import__("pathlib").Path(PARAMS),
        limits_path=__import__("pathlib").Path(LIMITS),
    )
    maze = MazeMap(
        wall_threshold_mm=params.get("tof.wall_threshold_mm"),
        heading=Direction.NORTH,
    )
    maze.cell_width_mm = 250
    maze.cell_height_mm = 450
    runner = MazeRunner(
        client=SerialClient(fake, timeout_s=0.01),
        params=params,
        maze=maze,
        planner=MazePlanner(),
        motion_targets=MotionTargetResolver(
            ticks_per_mm=5.4,
            fallback_cell_size_mm=250,
            turn_90_ticks=720,
            turn_180_ticks=1440,
        ),
    )

    result = runner.run_step()
    planned = next(
        event["payload"]
        for event in result.events
        if event["type"] == "planned_action"
    )

    assert sent_messages(fake)[0]["target_ticks"] == 2430
    assert planned["direction"] == "N"
    assert planned["distance_mm"] == 450.0
    assert planned["ticks_per_mm"] == 5.4
    assert planned["target_ticks"] == 2430
    assert planned["target_source"] == "map.cell_height_mm"


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


def test_runner_updates_pose_before_gate_and_advances_an_accepted_move_once():
    runner, _client, maze = evidence_runner(
        result={
            "type": "done",
            "name": "move_cell",
            "success": True,
            "duration_ms": 1000,
            "enc_left": 1350,
            "enc_right": 1350,
            "front_mm": 225,
            "left_mm": 225,
            "right_mm": 225,
            "imu_available": False,
        }
    )

    result = runner.run_step()
    event_types = [event["type"] for event in result.events]

    assert result.outcome == "continue"
    assert result.evidence["status"] == "accepted"
    assert result.reliable_pose["grid_cell"] == [0, 0]
    assert maze.position == (0, 0)
    assert event_types.index("pose.updated") < event_types.index(
        "motion_evidence"
    )


def test_malformed_recovery_result_does_not_advance_and_stops_unsafe():
    runner, client, maze = evidence_runner(
        result={
            "type": "done",
            "name": "move_cell",
            "success": True,
            "duration_ms": 1000,
            "enc_left": 1095,
            "enc_right": 1095,
            "front_mm": 310,
            "left_mm": 225,
            "right_mm": 225,
            "imu_available": False,
        }
    )

    result = runner.run_step()

    assert result.outcome == "unsafe"
    assert result.error_code == "ACTION_RESULT_MISMATCH"
    assert result.evidence["status"] == "recoverable"
    assert maze.position == (0, 1)
    assert len(client.executed) == 2


def test_map_sensor_conflict_blocks_transport_action_and_keeps_logical_pose():
    runner, client, maze = evidence_runner(
        result={},
        conflict_required_samples=1,
    )
    client.baseline["front_mm"] = 90

    result = runner.run_step()

    assert result.outcome == "unsafe"
    assert result.error_code == "MAP_SENSOR_CONFLICT"
    assert client.executed == []
    assert maze.position == (0, 1)


def test_sim_truth_is_emitted_only_as_evaluation_and_not_motion_evidence():
    runner, _client, _maze = evidence_runner(
        result={
            "type": "done",
            "name": "move_cell",
            "success": True,
            "duration_ms": 1000,
            "enc_left": 1350,
            "enc_right": 1350,
            "front_mm": 225,
            "left_mm": 225,
            "right_mm": 225,
            "imu_available": False,
            "sim_truth": {
                "x_mm": 225,
                "y_mm": 225,
                "yaw_deg": 0,
            },
        }
    )

    result = runner.run_step()
    evaluation = next(
        event for event in result.events if event["type"] == "sim.evaluation"
    )

    assert evaluation["payload"]["position_error_mm"] >= 0
    assert "sim_truth" not in str(result.evidence)


def test_runner_uses_fresh_subscribed_telemetry_when_done_has_only_encoders():
    params = ParamManager(
        params_path=__import__("pathlib").Path(PARAMS),
        limits_path=__import__("pathlib").Path(LIMITS),
    )
    maze = MazeMap.from_definition(
        corridor_definition(),
        wall_threshold_mm=150,
        map_version_id="corridor-v1",
    )
    baseline = {
        "type": "telemetry",
        "ts_ms": 0,
        "enc_left": 0,
        "enc_right": 0,
        "front_mm": 675,
        "left_mm": 225,
        "right_mm": 225,
        "imu_available": True,
        "imu_yaw_deg": 0,
    }
    client = SubscribedEvidenceClient(
        baseline,
        {
            "type": "done",
            "name": "move_cell",
            "success": True,
            "duration_ms": 1000,
            "enc_left": 1350,
            "enc_right": 1350,
        },
        {
            "type": "telemetry",
            "ts_ms": 1000,
            "state": "IDLE",
            "enc_left": 1350,
            "enc_right": 1350,
            "front_mm": 225,
            "left_mm": 225,
            "right_mm": 225,
            "imu_available": True,
            "imu_yaw_deg": 0,
            "sim_truth": {
                "x_mm": 225,
                "y_mm": 225,
                "yaw_deg": 0,
            },
        },
    )
    runner = MazeRunner(
        client=client,
        params=params,
        maze=maze,
        planner=FixedPlanner(
            PlannedAction("move_cell", Direction.NORTH)
        ),
        pose_tracker=TaskPoseTracker.from_params(
            maze=maze,
            params=params,
            arrival_config=ArrivalVerificationConfig(),
            run_id="run-subscribed",
        ),
    )

    result = runner.run_step()

    assert result.outcome == "continue"
    assert result.evidence["status"] == "accepted"
    assert maze.position == (0, 0)
    assert client.subscription.closed is True
    assert any(
        event["type"] == "completion.telemetry"
        for event in result.events
    )


def test_runner_executes_bounded_recovery_then_commits_original_cell_once():
    params = ParamManager(
        params_path=__import__("pathlib").Path(PARAMS),
        limits_path=__import__("pathlib").Path(LIMITS),
    )
    maze = MazeMap(wall_threshold_mm=150)
    client = RecoveryClient()
    tracker = ScriptedRecoveryTracker(["accepted"])
    runner = MazeRunner(
        client=client,
        params=params,
        maze=maze,
        planner=FixedPlanner(
            PlannedAction("move_cell", Direction.NORTH)
        ),
        pose_tracker=tracker,
    )

    result = runner.run_step()

    assert result.outcome == "continue"
    assert maze.position == (0, 1)
    assert tracker.commit_count == 1
    assert [item["name"] for item in client.executed] == [
        "move_cell",
        "nudge_forward",
    ]
    recovery = client.executed[1]
    assert recovery["action_id"] != client.executed[0]["action_id"]
    assert recovery["recovery"] is True
    assert recovery["parent_action_id"] == client.executed[0]["action_id"]
    assert recovery["speed"] <= params.get("motor.base_speed") * 0.5


def test_runner_stops_after_two_recoveries_without_advancing_grid():
    params = ParamManager(
        params_path=__import__("pathlib").Path(PARAMS),
        limits_path=__import__("pathlib").Path(LIMITS),
    )
    maze = MazeMap(wall_threshold_mm=150)
    client = RecoveryClient()
    tracker = ScriptedRecoveryTracker(
        ["recoverable", "recoverable"]
    )
    runner = MazeRunner(
        client=client,
        params=params,
        maze=maze,
        planner=FixedPlanner(
            PlannedAction("move_cell", Direction.NORTH)
        ),
        pose_tracker=tracker,
    )

    result = runner.run_step()

    assert result.outcome == "unsafe"
    assert result.error_code == "MOTION_RECOVERY_FAILED"
    assert maze.position == (0, 0)
    assert tracker.commit_count == 0
    assert len(client.executed) == 3
    assert len(
        {command["action_id"] for command in client.executed}
    ) == 3
