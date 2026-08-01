"""RDK-side maze exploration loop orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol

from .logger import JsonlLogger
from .maze_map import MazeMap, PlannedAction
from .maze_planner import MazePlanner
from .auto_tuner import AutoTuner
from .motion_analyzer import MotionAnalyzer, MotionReport
from .param_manager import ParamManager


class MazeClient(Protocol):
    def wait_telemetry(self) -> dict:
        ...

    def execute_action(
        self,
        *,
        action_id: str,
        name: str,
        speed: float,
        target_ticks: int,
    ) -> dict:
        ...


class StepControl(Protocol):
    def pause_requested(self) -> bool:
        ...

    def stop_requested(self) -> bool:
        ...


GoalCondition = Callable[[MazeMap, Mapping[str, Any]], bool]
EventSink = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class MazeStepResult:
    action: PlannedAction
    action_id: Optional[str]
    telemetry: dict
    done: Optional[dict]
    map_text: str
    motion_report: Optional[MotionReport] = None
    tune_event: Optional[dict] = None
    outcome: str = "continue"
    events: tuple[dict[str, Any], ...] = ()


class MazeRunner:
    def __init__(
        self,
        *,
        client: MazeClient,
        params: ParamManager,
        maze: MazeMap,
        planner: MazePlanner,
        analyzer: Optional[MotionAnalyzer] = None,
        tuner: Optional[AutoTuner] = None,
        logger: Optional[JsonlLogger] = None,
        action_prefix: str = "maze",
    ) -> None:
        self.client = client
        self.params = params
        self.maze = maze
        self.planner = planner
        self.analyzer = analyzer
        self.tuner = tuner
        self.logger = logger
        self.action_prefix = action_prefix
        self._action_index = 0

    def run_step(
        self,
        *,
        control: StepControl | None = None,
        goal: GoalCondition | None = None,
        event_sink: EventSink | None = None,
    ) -> MazeStepResult:
        events: list[dict[str, Any]] = []

        def emit(
            event_type: str,
            payload: Any,
            *,
            legacy_log: bool = True,
        ) -> None:
            event = {"type": event_type, "payload": _json_ready(payload)}
            events.append(event)
            if event_sink is not None:
                event_sink(event)
            if legacy_log:
                self._log(event_type, payload)

        emit(
            "step.started",
            {"next_action_index": self._action_index + 1},
            legacy_log=False,
        )
        cancellation = self._cancellation_outcome(control)
        if cancellation is not None:
            emit(
                f"step.{cancellation}",
                {"before_transport": True},
                legacy_log=False,
            )
            return MazeStepResult(
                action=PlannedAction(
                    "pause" if cancellation == "paused" else "stop"
                ),
                action_id=None,
                telemetry={},
                done=None,
                map_text=self.maze.render_ascii(),
                outcome=cancellation,
                events=tuple(events),
            )

        telemetry = self.client.wait_telemetry()
        emit("telemetry", telemetry)
        self.maze.observe(
            front_mm=int(telemetry["front_mm"]),
            left_mm=int(telemetry["left_mm"]),
            right_mm=int(telemetry["right_mm"]),
        )

        if goal is not None and goal(self.maze, telemetry):
            emit("maze_update", self.maze.to_dict())
            emit(
                "step.goal_reached",
                {"position": list(self.maze.position)},
                legacy_log=False,
            )
            return MazeStepResult(
                action=PlannedAction("stop"),
                action_id=None,
                telemetry=telemetry,
                done=None,
                map_text=self.maze.render_ascii(),
                outcome="goal_reached",
                events=tuple(events),
            )

        cancellation = self._cancellation_outcome(control)
        if cancellation is not None:
            emit(
                f"step.{cancellation}",
                {"before_action": True},
                legacy_log=False,
            )
            return MazeStepResult(
                action=PlannedAction(
                    "pause" if cancellation == "paused" else "stop"
                ),
                action_id=None,
                telemetry=telemetry,
                done=None,
                map_text=self.maze.render_ascii(),
                outcome=cancellation,
                events=tuple(events),
            )

        action = self.planner.next_action(self.maze)
        if action.name == "stop":
            emit("planned_action", action)
            emit("maze_update", self.maze.to_dict())
            emit(
                "step.exhausted",
                {"position": list(self.maze.position)},
                legacy_log=False,
            )
            return MazeStepResult(
                action=action,
                action_id=None,
                telemetry=telemetry,
                done=None,
                map_text=self.maze.render_ascii(),
                outcome="exhausted",
                events=tuple(events),
            )

        action_id = self._next_action_id()
        speed, target_ticks = self._motion_params_for(action)
        emit(
            "planned_action",
            {
                "action_id": action_id,
                "name": action.name,
                "speed": speed,
                "target_ticks": target_ticks,
            },
        )
        done = self.client.execute_action(
            action_id=action_id,
            name=action.name,
            speed=speed,
            target_ticks=target_ticks,
        )
        emit("done", done)
        self.maze.apply_completed_action(action)
        motion_report = None
        tune_event = None
        if self.analyzer is not None:
            motion_report = self.analyzer.analyze(action_name=action.name, target_ticks=target_ticks, result=done)
            emit("motion_report", motion_report)
            if self.tuner is not None:
                tune_event = self.tuner.apply(motion_report)
                emit("param_change", tune_event)
        emit("maze_update", self.maze.to_dict())
        outcome = (
            "goal_reached"
            if goal is not None and goal(self.maze, done)
            else "continue"
        )
        emit(
            f"step.{outcome}",
            {
                "action_id": action_id,
                "position": list(self.maze.position),
            },
            legacy_log=False,
        )
        return MazeStepResult(
            action=action,
            action_id=action_id,
            telemetry=telemetry,
            done=done,
            map_text=self.maze.render_ascii(),
            motion_report=motion_report,
            tune_event=tune_event,
            outcome=outcome,
            events=tuple(events),
        )

    def _next_action_id(self) -> str:
        self._action_index += 1
        return f"{self.action_prefix}-{self._action_index:04d}"

    def _motion_params_for(self, action: PlannedAction) -> tuple[float, int]:
        if action.name == "move_cell":
            return float(self.params.get("motor.base_speed")), int(self.params.get("motion.cell_ticks"))
        if action.name == "turn_back":
            return float(self.params.get("motor.turn_speed")), int(self.params.get("motion.turn_180_ticks"))
        return float(self.params.get("motor.turn_speed")), int(self.params.get("motion.turn_90_ticks"))

    def _log(self, event_type: str, payload: object) -> None:
        if self.logger is not None:
            self.logger.record(event_type, payload)

    @staticmethod
    def _cancellation_outcome(
        control: StepControl | None,
    ) -> str | None:
        if control is None:
            return None
        if control.stop_requested():
            return "stopped"
        if control.pause_requested():
            return "paused"
        return None


def _json_ready(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return _json_ready(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    return value
