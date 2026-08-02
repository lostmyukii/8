"""RDK-side maze exploration loop orchestration."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol

from .logger import JsonlLogger
from .maze_map import MazeMap, PlannedAction
from .auto_tuner import AutoTuner
from .motion_analyzer import MotionAnalyzer, MotionReport
from .motion_targets import MotionTarget, MotionTargetResolver
from .param_manager import ParamManager
from .pose_types import TruthPose, evaluate_pose
from .protocol import (
    ProtocolError,
    extract_fusion_telemetry,
    extract_simulation_truth,
)
from .task_pose_tracker import (
    TaskPoseTracker,
    TaskPoseTrackerError,
)


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


class ActionPlanner(Protocol):
    def next_action(self, maze: MazeMap) -> PlannedAction:
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
    motion_target: Optional[MotionTarget] = None
    outcome: str = "continue"
    events: tuple[dict[str, Any], ...] = ()
    evidence: Optional[dict[str, Any]] = None
    reliable_pose: Optional[dict[str, Any]] = None
    error_code: Optional[str] = None


class MazeRunner:
    def __init__(
        self,
        *,
        client: MazeClient,
        params: ParamManager,
        maze: MazeMap,
        planner: ActionPlanner,
        analyzer: Optional[MotionAnalyzer] = None,
        tuner: Optional[AutoTuner] = None,
        logger: Optional[JsonlLogger] = None,
        action_prefix: str = "maze",
        motion_targets: MotionTargetResolver | None = None,
        pose_tracker: TaskPoseTracker | None = None,
    ) -> None:
        self.client = client
        self.params = params
        self.maze = maze
        self.planner = planner
        self.analyzer = analyzer
        self.tuner = tuner
        self.logger = logger
        self.action_prefix = action_prefix
        self.motion_targets = motion_targets
        self.pose_tracker = pose_tracker
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
        if self.pose_tracker is not None:
            conflict = self.pose_tracker.check_map_conflict(telemetry)
            if conflict is not None:
                conflict_payload = conflict.to_dict()
                evidence = {
                    "status": "unsafe",
                    "code": conflict.code,
                    "reasons": [
                        "live wall evidence conflicts with immutable map"
                    ],
                }
                emit("map_sensor_conflict", conflict_payload)
                emit("motion_evidence", evidence)
                emit(
                    "step.unsafe",
                    {"error_code": conflict.code},
                    legacy_log=False,
                )
                return MazeStepResult(
                    action=PlannedAction("stop"),
                    action_id=None,
                    telemetry=telemetry,
                    done=None,
                    map_text=self.maze.render_ascii(),
                    outcome="unsafe",
                    events=tuple(events),
                    evidence=evidence,
                    reliable_pose=(
                        self.pose_tracker.estimate().to_dict()
                    ),
                    error_code=conflict.code,
                )
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
        consume_route_event = getattr(
            self.planner,
            "consume_route_event",
            None,
        )
        if callable(consume_route_event):
            route_event = consume_route_event()
            if route_event is not None:
                emit("route.planned", route_event)
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
        speed, motion_target = self._motion_params_for(action)
        target_ticks = motion_target.target_ticks
        emit(
            "planned_action",
            {
                "action_id": action_id,
                "name": action.name,
                "speed": speed,
                **motion_target.to_dict(),
            },
        )
        if self.pose_tracker is not None:
            baseline = self.pose_tracker.begin_action(
                action_id=action_id,
                action=action,
                telemetry=telemetry,
            )
            emit("pose.baseline", baseline)
        completion_subscription = (
            self._subscribe_completion_telemetry()
            if self.pose_tracker is not None
            else None
        )
        completion_telemetry = None
        try:
            done = self.client.execute_action(
                action_id=action_id,
                name=action.name,
                speed=speed,
                target_ticks=target_ticks,
            )
            completion_telemetry = (
                self._wait_completion_telemetry(
                    completion_subscription,
                    done,
                )
            )
        finally:
            close_subscription = getattr(
                completion_subscription,
                "close",
                None,
            )
            if callable(close_subscription):
                close_subscription()
        emit("done", done)
        evidence_result = dict(done)
        if completion_telemetry is not None:
            emit(
                "completion.telemetry",
                extract_fusion_telemetry(completion_telemetry),
            )
            evidence_result = {
                **completion_telemetry,
                **done,
            }
        evidence_payload = None
        reliable_pose = None
        if self.pose_tracker is not None:
            try:
                tracked = self.pose_tracker.complete_action(
                    action_id=action_id,
                    action=action,
                    result=evidence_result,
                    motion_target=motion_target,
                )
            except TaskPoseTrackerError as exc:
                evidence_payload = {
                    "status": "unsafe",
                    "code": exc.code,
                    "reasons": [exc.message],
                }
                emit("motion_evidence", evidence_payload)
                emit(
                    "step.unsafe",
                    {
                        "action_id": action_id,
                        "error_code": exc.code,
                    },
                    legacy_log=False,
                )
                return MazeStepResult(
                    action=action,
                    action_id=action_id,
                    telemetry=telemetry,
                    done=done,
                    map_text=self.maze.render_ascii(),
                    motion_target=motion_target,
                    outcome="unsafe",
                    events=tuple(events),
                    evidence=evidence_payload,
                    reliable_pose=(
                        self.pose_tracker.estimate().to_dict()
                    ),
                    error_code=exc.code,
                )
            emit("pose.updated", tracked.pose)
            evidence_payload = tracked.decision.to_dict()
            emit("motion_evidence", evidence_payload)
            self._emit_truth_evaluation(
                evidence_result,
                tracked.pose,
                action_id=action_id,
                emit=emit,
            )
            if tracked.decision.status != "accepted":
                outcome = (
                    "recovery_required"
                    if tracked.decision.status == "recoverable"
                    else "unsafe"
                )
                error_code = (
                    "MOTION_RECOVERY_REQUIRED"
                    if outcome == "recovery_required"
                    else tracked.decision.code
                    or "MOTION_EVIDENCE_UNSAFE"
                )
                emit(
                    f"step.{outcome}",
                    {
                        "action_id": action_id,
                        "error_code": error_code,
                    },
                    legacy_log=False,
                )
                return MazeStepResult(
                    action=action,
                    action_id=action_id,
                    telemetry=telemetry,
                    done=done,
                    map_text=self.maze.render_ascii(),
                    motion_target=motion_target,
                    outcome=outcome,
                    events=tuple(events),
                    evidence=evidence_payload,
                    reliable_pose=tracked.pose.to_dict(),
                    error_code=error_code,
                )
            self.maze.apply_completed_action(action)
            reliable_pose = self.pose_tracker.accept_action(
                action
            ).to_dict()
            emit("pose.committed", reliable_pose)
        else:
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
            motion_target=motion_target,
            outcome=outcome,
            events=tuple(events),
            evidence=evidence_payload,
            reliable_pose=reliable_pose,
        )

    def _next_action_id(self) -> str:
        self._action_index += 1
        return f"{self.action_prefix}-{self._action_index:04d}"

    def _motion_params_for(
        self,
        action: PlannedAction,
    ) -> tuple[float, MotionTarget]:
        resolver = (
            self.motion_targets
            if self.motion_targets is not None
            else MotionTargetResolver.from_params(self.params)
        )
        target = resolver.resolve(action, self.maze)
        if action.name == "move_cell":
            speed = float(self.params.get("motor.base_speed"))
        else:
            speed = float(self.params.get("motor.turn_speed"))
        return speed, target

    def _log(self, event_type: str, payload: object) -> None:
        if self.logger is not None:
            self.logger.record(event_type, payload)

    def _subscribe_completion_telemetry(self):
        subscribe = getattr(self.client, "subscribe", None)
        if not callable(subscribe):
            return None
        return subscribe(
            message_types={"telemetry"},
            max_queue=128,
        )

    def _wait_completion_telemetry(
        self,
        subscription,
        done: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        if subscription is None:
            return None
        timeout_s = min(
            2.0,
            max(0.05, float(getattr(self.client, "timeout_s", 1.0))),
        )
        deadline = time.monotonic() + timeout_s
        expected_left = done.get("enc_left")
        expected_right = done.get("enc_right")
        while time.monotonic() < deadline:
            message = subscription.get(
                timeout_s=max(0.0, deadline - time.monotonic())
            )
            if message is None:
                break
            if (
                expected_left is None
                or expected_right is None
                or (
                    message.get("enc_left") == expected_left
                    and message.get("enc_right") == expected_right
                )
            ):
                return message
        return None

    @staticmethod
    def _emit_truth_evaluation(
        result: Mapping[str, Any],
        estimate,
        *,
        action_id: str,
        emit,
    ) -> None:
        try:
            truth = extract_simulation_truth(result)
            if truth is None:
                return
            evaluation = evaluate_pose(
                estimate,
                TruthPose.from_mapping(truth),
            )
        except (ProtocolError, ValueError, KeyError):
            return
        emit(
            "sim.evaluation",
            {
                "action_id": action_id,
                **evaluation.to_dict(),
            },
        )

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
