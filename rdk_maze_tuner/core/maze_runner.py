"""RDK-side maze exploration loop orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .logger import JsonlLogger
from .maze_map import MazeMap, PlannedAction
from .maze_planner import MazePlanner
from .auto_tuner import AutoTuner
from .motion_analyzer import MotionAnalyzer, MotionReport
from .param_manager import ParamManager
from .serial_client import SerialClient


@dataclass(frozen=True)
class MazeStepResult:
    action: PlannedAction
    action_id: Optional[str]
    telemetry: dict
    done: Optional[dict]
    map_text: str
    motion_report: Optional[MotionReport] = None
    tune_event: Optional[dict] = None


class MazeRunner:
    def __init__(
        self,
        *,
        client: SerialClient,
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

    def run_step(self) -> MazeStepResult:
        telemetry = self.client.wait_telemetry()
        self._log("telemetry", telemetry)
        self.maze.observe(
            front_mm=int(telemetry["front_mm"]),
            left_mm=int(telemetry["left_mm"]),
            right_mm=int(telemetry["right_mm"]),
        )

        action = self.planner.next_action(self.maze)
        if action.name == "stop":
            self._log("planned_action", action)
            self._log("maze_update", self.maze.to_dict())
            return MazeStepResult(
                action=action,
                action_id=None,
                telemetry=telemetry,
                done=None,
                map_text=self.maze.render_ascii(),
            )

        action_id = self._next_action_id()
        speed, target_ticks = self._motion_params_for(action)
        self._log(
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
        self._log("done", done)
        self.maze.apply_completed_action(action)
        motion_report = None
        tune_event = None
        if self.analyzer is not None:
            motion_report = self.analyzer.analyze(action_name=action.name, target_ticks=target_ticks, result=done)
            self._log("motion_report", motion_report)
            if self.tuner is not None:
                tune_event = self.tuner.apply(motion_report)
                self._log("param_change", tune_event)
        self._log("maze_update", self.maze.to_dict())
        return MazeStepResult(
            action=action,
            action_id=action_id,
            telemetry=telemetry,
            done=done,
            map_text=self.maze.render_ascii(),
            motion_report=motion_report,
            tune_event=tune_event,
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
