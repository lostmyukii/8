from __future__ import annotations

import json
import threading
from collections import deque

import pytest

from rdk_maze_tuner.agent.runtime import (
    AgentRuntime,
    AgentRuntimeState,
    build_task_envelope,
)
from rdk_maze_tuner.core.device_session import (
    DeviceDisconnectedError,
    DeviceSession,
    DeviceSessionTimeout,
)
from rdk_maze_tuner.core.goal_directed_planner import GoalDirectedPlanner
from rdk_maze_tuner.core.goal_verifier import GoalVerifier
from rdk_maze_tuner.core.maze_definition import (
    MapDefinition,
    StartPose,
    WallSegment,
)
from rdk_maze_tuner.core.motion_evidence import ArrivalVerificationConfig
from rdk_maze_tuner.core.task_pose_tracker import TaskPoseTracker
from rdk_maze_tuner.core.serial_client import (
    SerialClient,
    SerialClientError,
)


def map_definition():
    rows = cols = 5
    return MapDefinition(
        rows=rows,
        cols=cols,
        cell_width_mm=250,
        cell_height_mm=250,
        wall_thickness_mm=40,
        wall_height_mm=180,
        start=StartPose(0, 4, "N"),
        goals=((4, 0),),
        walls=(
            *(WallSegment(x, 0, x + 1, 0) for x in range(cols)),
            *(WallSegment(x, rows, x + 1, rows) for x in range(cols)),
            *(WallSegment(0, y, 0, y + 1) for y in range(rows)),
            *(WallSegment(cols, y, cols, y + 1) for y in range(rows)),
        ),
    )


def params():
    return {
        "robot": {
            "cell_size_cm": 25,
            "wheel_diameter_cm": 6.5,
            "wheel_base_cm": 13.5,
        },
        "motor": {"base_speed": 0.25, "turn_speed": 0.18},
        "motion": {
            "cell_ticks": 1350,
            "turn_90_ticks": 720,
            "turn_180_ticks": 1440,
        },
        "tof": {"wall_threshold_mm": 150},
    }


def envelope():
    definition = map_definition()
    return build_task_envelope(
        task_id="task-real-1",
        run_id="run-real-1",
        map_version_id="map-v2",
        map_digest=definition.content_digest,
        map_definition=definition.to_dict(),
        goal={
            "type": "map_goal",
            "cell": [4, 0],
            "source_map_version": "map-v2",
            "source_map_digest": definition.content_digest,
        },
        param_version_id="param-v1",
        param_digest="b" * 64,
        param_snapshot=params(),
        arrival_verification=ArrivalVerificationConfig().to_dict(),
        max_steps=50,
    )


class FakeLocalSession:
    """Task-level fake for the RDK-owned serial action loop."""

    def __init__(
        self,
        *,
        disconnect_after_actions=None,
        action_fault=None,
    ):
        self.cell = [0, 4]
        self.heading = "N"
        self.enc_left = 0
        self.enc_right = 0
        self.actions = []
        self.stops = 0
        self.disconnect_after_actions = disconnect_after_actions
        self.action_fault = action_fault

    def start(self):
        return None

    def wait_ready(self, *, timeout_s=None):
        return {"type": "ready", "fw": "maze-esp32", "version": "0.1.0"}

    def wait_telemetry(self, *, timeout_s=None):
        return self._telemetry()

    def execute_action(self, **command):
        if self.action_fault == "timeout":
            raise DeviceSessionTimeout("fake action timeout")
        if self.action_fault == "front_too_close":
            raise SerialClientError(
                "OBSTACLE_TOO_CLOSE: front distance unsafe"
            )
        if (
            self.disconnect_after_actions is not None
            and len(self.actions) >= self.disconnect_after_actions
        ):
            raise DeviceDisconnectedError("fake serial disconnected")
        self.actions.append(dict(command))
        ticks = int(command["target_ticks"])
        name = command["name"]
        if name == "move_cell":
            self.enc_left += ticks
            self.enc_right += ticks
            dx, dy = {
                "N": (0, -1),
                "E": (1, 0),
                "S": (0, 1),
                "W": (-1, 0),
            }[self.heading]
            self.cell[0] += dx
            self.cell[1] += dy
        elif name == "turn_right":
            self.enc_left += ticks
            self.enc_right -= ticks
            self.heading = {"N": "E", "E": "S", "S": "W", "W": "N"}[
                self.heading
            ]
        elif name == "turn_left":
            self.enc_left -= ticks
            self.enc_right += ticks
            self.heading = {"N": "W", "W": "S", "S": "E", "E": "N"}[
                self.heading
            ]
        elif name == "turn_back":
            self.enc_left += ticks
            self.enc_right -= ticks
            self.heading = {"N": "S", "S": "N", "E": "W", "W": "E"}[
                self.heading
            ]
        return {
            **self._telemetry(),
            "type": "done",
            "action_id": command["action_id"],
            "name": name,
            "success": True,
            "duration_ms": 500,
        }

    def stop(self):
        self.stops += 1
        return {"type": "ack", "ok": True, "command": "stop"}

    def estop(self, *, reason):
        self.stops += 1
        return {"type": "ack", "ok": True, "command": "estop"}

    def close(self):
        return None

    def _telemetry(self):
        x, y = self.cell
        distances = {
            "N": (y + 0.5) * 250,
            "E": (5 - x - 0.5) * 250,
            "S": (5 - y - 0.5) * 250,
            "W": (x + 0.5) * 250,
        }
        order = ("N", "E", "S", "W")
        index = order.index(self.heading)
        return {
            "type": "telemetry",
            "state": "IDLE",
            "ts_ms": len(self.actions) * 500,
            "enc_left": self.enc_left,
            "enc_right": self.enc_right,
            "front_mm": distances[order[index]],
            "left_mm": distances[order[(index - 1) % 4]],
            "right_mm": distances[order[(index + 1) % 4]],
            "imu_available": True,
            "imu_yaw_deg": {"N": 0, "E": 90, "S": 180, "W": 270}[
                self.heading
            ],
        }


class InteractiveMazeSerial:
    def __init__(self):
        self.model = FakeLocalSession()
        self._condition = threading.Condition()
        self._lines = deque()
        self._closed = False
        self.active_readers = 0
        self.max_active_readers = 0
        self.feed(
            {"type": "ready", "fw": "maze-esp32", "version": "0.1.0"}
        )
        self.feed(self.model._telemetry())

    def write(self, data):
        message = json.loads(data.decode("utf-8"))
        if message["type"] == "action":
            result = self.model.execute_action(
                action_id=message["action_id"],
                name=message["name"],
                speed=message["speed"],
                target_ticks=message["target_ticks"],
                recovery=message.get("recovery", False),
                direction=message.get("direction"),
                parent_action_id=message.get("parent_action_id"),
            )
            self.feed(
                {"type": "ack", "seq": message["seq"], "ok": True}
            )
            self.feed(result)
            self.feed(self.model._telemetry())
        elif message["type"] == "stop":
            self.model.stop()
            self.feed(
                {"type": "ack", "seq": message["seq"], "ok": True}
            )
            self.feed(self.model._telemetry())
        elif message["type"] == "estop":
            self.model.estop(reason=message.get("reason", "test"))
            self.feed(
                {"type": "ack", "seq": message["seq"], "ok": True}
            )
        else:
            self.feed(
                {"type": "ack", "seq": message["seq"], "ok": True}
            )
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
                    lambda: self._lines or self._closed,
                    timeout=0.01,
                )
                if self._lines:
                    return self._lines.popleft()
                if self._closed:
                    raise OSError("fake serial closed")
                return b""
            finally:
                self.active_readers -= 1

    def feed(self, message):
        line = (
            json.dumps(message, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        with self._condition:
            self._lines.append(line)
            self._condition.notify_all()

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()


def test_agent_uses_shared_local_navigation_core_and_reaches_map_goal():
    events = []
    stream = InteractiveMazeSerial()
    session = DeviceSession(
        SerialClient(stream, timeout_s=0.5),
        action_result_timeout_s=0.5,
    )
    runtime = AgentRuntime(session=session, event_sink=events.append)

    prepared = runtime.prepare(envelope())
    result = runtime.run()

    assert isinstance(runtime.session, DeviceSession)
    assert isinstance(runtime.planner, GoalDirectedPlanner)
    assert isinstance(runtime.pose_tracker, TaskPoseTracker)
    assert isinstance(runtime.goal_verifier, GoalVerifier)
    assert prepared["goal"]["cell"] == [4, 0]
    assert result["status"] == "COMPLETED"
    assert result["result"] == "goal_verified"
    assert result["pose"]["grid_cell"] == [4, 0]
    assert stream.model.cell == [4, 0]
    assert stream.max_active_readers == 1
    assert all(
        "left_pwm" not in event and "right_pwm" not in event
        for event in events
    )
    assert {event["type"] for event in events} >= {
        "task.status",
        "task.event",
        "task.result",
    }
    runtime.close()


def test_cloud_disconnect_stops_locally_enters_lost_and_never_resumes():
    session = FakeLocalSession()
    runtime = AgentRuntime(session=session, event_sink=lambda _event: None)
    runtime.prepare(envelope())

    lost = runtime.on_cloud_disconnect()

    assert session.stops == 1
    assert lost["status"] == "LOST"
    assert runtime.state is AgentRuntimeState.LOST
    with pytest.raises(RuntimeError, match="LOST"):
        runtime.run()


@pytest.mark.parametrize(
    "fault",
    ["serial_disconnect", "action_timeout", "front_too_close", "estop"],
)
def test_local_faults_stop_before_the_runtime_reports_terminal_state(fault):
    session = FakeLocalSession(
        disconnect_after_actions=(
            0 if fault == "serial_disconnect" else None
        ),
        action_fault=(
            "timeout"
            if fault == "action_timeout"
            else "front_too_close"
            if fault == "front_too_close"
            else None
        ),
    )
    events = []
    runtime = AgentRuntime(session=session, event_sink=events.append)
    runtime.prepare(envelope())

    if fault == "estop":
        terminal = runtime.estop(reason="dashboard")
    else:
        terminal = runtime.run()

    assert session.stops >= 1
    assert terminal["status"] in {"LOST", "ERROR", "ESTOP"}
    assert events[-1]["type"] in {"task.result", "task.status"}
