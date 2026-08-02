"""Run-scoped pose fusion and motion-arrival evidence for maze tasks."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping

from .map_sensor_conflict import (
    MapSensorConflict,
    MapSensorConflictDetector,
)
from .maze_map import MazeMap, ORDER, PlannedAction
from .motion_evidence import (
    MOTION_EVIDENCE_UNSAFE,
    ArrivalVerificationConfig,
    MotionEvidenceDecision,
    MotionEvidenceGate,
    MotionEvidenceInput,
)
from .motion_targets import MotionTarget
from .pose_fusion import PoseFusion
from .pose_types import (
    HEADING_YAW_DEG,
    PoseEstimate,
    PoseFusionConfig,
    PoseObservation,
    angle_delta_deg,
)
from .protocol import extract_fusion_telemetry
from .slip_estimator import (
    SlipEstimate,
    SlipEstimator,
    SlipEstimatorConfig,
)
from .wall_evidence import (
    WallEvidenceBuilder,
    WallEvidenceSnapshot,
    local_to_global,
)


ACTION_RESULT_INVALID = "ACTION_RESULT_INVALID"
ACTION_RESULT_MISMATCH = "ACTION_RESULT_MISMATCH"
ACTION_NOT_ACTIVE = "ACTION_NOT_ACTIVE"
ACTION_ALREADY_COMMITTED = "ACTION_ALREADY_COMMITTED"


class TaskPoseTrackerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ActionPoseBaseline:
    action_id: str
    action_name: str
    enc_left: int
    enc_right: int
    pose: PoseEstimate
    wall_evidence: WallEvidenceSnapshot

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "action_name": self.action_name,
            "enc_left": self.enc_left,
            "enc_right": self.enc_right,
            "pose": self.pose.to_dict(),
            "wall_constraint_count": len(
                self.wall_evidence.constraints
            ),
        }


@dataclass(frozen=True)
class TrackedMotionResult:
    evidence: MotionEvidenceInput
    decision: MotionEvidenceDecision
    pose: PoseEstimate
    slip: SlipEstimate
    expected_cell: tuple[int, int]
    expected_heading: str
    external_evidence_available: bool
    independent_wall_constraints: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence": asdict(self.evidence),
            "decision": self.decision.to_dict(),
            "pose": self.pose.to_dict(),
            "slip": self.slip.to_dict(),
            "expected_cell": [
                self.expected_cell[0],
                self.expected_cell[1],
            ],
            "expected_heading": self.expected_heading,
            "external_evidence_available": (
                self.external_evidence_available
            ),
            "independent_wall_constraints": (
                self.independent_wall_constraints
            ),
        }


class TaskPoseTracker:
    """Own one estimator and evidence gate for exactly one task run."""

    def __init__(
        self,
        *,
        maze: MazeMap,
        fusion_config: PoseFusionConfig,
        arrival_config: ArrivalVerificationConfig,
        run_id: str | None,
        conflict_required_samples: int = 3,
    ) -> None:
        self.maze = maze
        self.run_id = run_id
        self.fusion = PoseFusion(
            config=fusion_config,
            initial_cell=maze.position,
            initial_heading=maze.heading.value,
        )
        self.slip_estimator = SlipEstimator(
            SlipEstimatorConfig(
                mm_per_tick=fusion_config.mm_per_tick,
                wheel_base_mm=fusion_config.wheel_base_mm,
            )
        )
        self.gate = MotionEvidenceGate(arrival_config)
        self.wall_evidence = WallEvidenceBuilder(
            maze=maze,
            fallback_cell_width_mm=fusion_config.cell_width_mm,
            fallback_cell_height_mm=fusion_config.cell_height_mm,
        )
        self._conflict_detectors = {
            local: MapSensorConflictDetector(
                required_consecutive_samples=(
                    conflict_required_samples
                )
            )
            for local in ("front", "left", "right")
        }
        for detector in self._conflict_detectors.values():
            detector.reset(run_id=run_id)
        self.last_baseline: ActionPoseBaseline | None = None
        self.last_result: TrackedMotionResult | None = None
        self.last_fusion_input: dict[str, Any] = {}
        self._baseline_fusion_input: dict[str, Any] = {}
        self._committed = False

    @classmethod
    def from_params(
        cls,
        *,
        maze: MazeMap,
        params,
        arrival_config: ArrivalVerificationConfig,
        run_id: str | None,
        conflict_required_samples: int = 3,
    ) -> "TaskPoseTracker":
        fallback_cell_mm = (
            float(params.get("robot.cell_size_cm")) * 10.0
        )
        return cls(
            maze=maze,
            fusion_config=PoseFusionConfig(
                cell_width_mm=float(
                    maze.cell_width_mm or fallback_cell_mm
                ),
                cell_height_mm=float(
                    maze.cell_height_mm or fallback_cell_mm
                ),
                cell_ticks=float(params.get("motion.cell_ticks")),
                turn_90_ticks=float(
                    params.get("motion.turn_90_ticks")
                ),
                wheel_base_mm=(
                    float(params.get("robot.wheel_base_cm")) * 10.0
                ),
                y_axis_down=bool(
                    getattr(maze, "_screen_coordinates", False)
                ),
            ),
            arrival_config=arrival_config,
            run_id=run_id,
            conflict_required_samples=conflict_required_samples,
        )

    def estimate(self) -> PoseEstimate:
        return self.fusion.estimate()

    def check_map_conflict(
        self,
        telemetry: Mapping[str, Any],
    ) -> MapSensorConflict | None:
        for local, raw_field in (
            ("front", "front_mm"),
            ("left", "left_mm"),
            ("right", "right_mm"),
        ):
            direction = local_to_global(self.maze.heading, local)
            planned_wall = self.maze.cell(
                self.maze.position
            ).planned_walls[direction.value]
            conflict = self._conflict_detectors[local].observe(
                cell=self.maze.position,
                direction=direction,
                planned_wall=planned_wall,
                distance_mm=telemetry.get(raw_field),
                wall_threshold_mm=self.maze.wall_threshold_mm,
            )
            if conflict is not None:
                return conflict
        return None

    def begin_action(
        self,
        *,
        action_id: str,
        action: PlannedAction,
        telemetry: Mapping[str, Any],
    ) -> ActionPoseBaseline:
        fusion_input = dict(extract_fusion_telemetry(telemetry))
        if "ts_ms" not in fusion_input and "uptime_ms" not in fusion_input:
            fusion_input["ts_ms"] = 0
        observation = PoseObservation.from_mapping(fusion_input)
        walls = self.wall_evidence.build(
            fusion_input,
            cell=self.maze.position,
            heading=self.maze.heading,
        )
        pose = self.fusion.update(
            observation,
            wall_constraints=walls.constraints,
        )
        self.slip_estimator.update(
            timestamp_ms=observation.timestamp_ms,
            enc_left=observation.enc_left,
            enc_right=observation.enc_right,
            imu_available=observation.imu_available,
            imu_yaw_deg=observation.imu_yaw_deg,
            external_distance_mm=None,
        )
        baseline = ActionPoseBaseline(
            action_id=str(action_id),
            action_name=action.name,
            enc_left=observation.enc_left,
            enc_right=observation.enc_right,
            pose=pose,
            wall_evidence=walls,
        )
        self.last_baseline = baseline
        self.last_result = None
        self.last_fusion_input = fusion_input
        self._baseline_fusion_input = fusion_input
        self._committed = False
        return baseline

    def complete_action(
        self,
        *,
        action_id: str,
        action: PlannedAction,
        result: Mapping[str, Any],
        motion_target: MotionTarget,
    ) -> TrackedMotionResult:
        baseline = self._require_baseline(action_id, action)
        result_type = result.get("type")
        if result_type not in {"done", "error"}:
            raise TaskPoseTrackerError(
                ACTION_RESULT_INVALID,
                "action result must be done or error",
            )
        if result.get("action_id") != action_id:
            raise TaskPoseTrackerError(
                ACTION_RESULT_MISMATCH,
                "action result action_id does not match active action",
            )
        if result.get("name") not in {None, action.name}:
            raise TaskPoseTrackerError(
                ACTION_RESULT_MISMATCH,
                "action result name does not match active action",
            )

        expected_cell, expected_heading = _expected_pose(
            self.maze,
            action,
        )
        fusion_input = dict(self._baseline_fusion_input)
        fusion_input.update(extract_fusion_telemetry(result))
        result_fusion_input = extract_fusion_telemetry(result)
        if (
            "ts_ms" not in result_fusion_input
            and "uptime_ms" not in result_fusion_input
        ):
            fusion_input["ts_ms"] = (
                int(self._baseline_fusion_input.get("ts_ms") or 0)
                + int(result.get("duration_ms") or 1)
            )
        observation = PoseObservation.from_mapping(fusion_input)
        walls = self.wall_evidence.build(
            fusion_input,
            cell=expected_cell,
            heading=expected_heading,
        )
        pose = self.fusion.update(
            observation,
            wall_constraints=walls.constraints,
        )
        external_distance = (
            self.wall_evidence.longitudinal_displacement(
                baseline.wall_evidence,
                walls,
            )
            if action.name == "move_cell"
            else None
        )
        slip = self.slip_estimator.update(
            timestamp_ms=observation.timestamp_ms,
            enc_left=observation.enc_left,
            enc_right=observation.enc_right,
            imu_available=observation.imu_available,
            imu_yaw_deg=observation.imu_yaw_deg,
            external_distance_mm=external_distance,
        )
        encoder_displacement = (
            (
                abs(observation.enc_left - baseline.enc_left)
                + abs(observation.enc_right - baseline.enc_right)
            )
            / 2.0
            * self.fusion.config.mm_per_tick
        )
        expected_distance = (
            float(motion_target.distance_mm or 0.0)
            if action.name == "move_cell"
            else 0.0
        )
        measured_distance = (
            float(external_distance)
            if external_distance is not None
            else (
                float(encoder_displacement)
                if action.name == "move_cell"
                else 0.0
            )
        )
        expected_yaw = HEADING_YAW_DEG[expected_heading]
        stable_heading = (
            abs(angle_delta_deg(expected_yaw, pose.yaw_deg))
            <= self.gate.config.recoverable_heading_error_deg
        )
        pose = self.fusion.qualify_task_estimate(
            pose,
            action_success=(
                result_type == "done"
                and result.get("success") is not False
            ),
            stable_grid_heading=stable_heading,
            independent_wall_constraints=walls.independent_axes,
        )
        evidence = MotionEvidenceInput(
            action_id=action_id,
            action_name=action.name,
            expected_distance_mm=expected_distance,
            measured_distance_mm=measured_distance,
            encoder_displacement_mm=float(encoder_displacement),
            external_displacement_mm=float(
                external_distance
                if external_distance is not None
                else 0.0
            ),
            expected_heading_deg=expected_yaw,
            measured_heading_deg=pose.yaw_deg,
            pose_confidence=pose.confidence,
            recovery_attempts=0,
            correction_evidence_available=(
                pose.continuous_heading_valid
                or walls.independent_axes >= 1
            ),
        )
        if result_type == "error" or result.get("success") is False:
            decision = MotionEvidenceDecision(
                status="unsafe",
                code=str(
                    result.get("code") or MOTION_EVIDENCE_UNSAFE
                ),
                position_error_ratio=0.0,
                heading_error_deg=abs(
                    angle_delta_deg(expected_yaw, pose.yaw_deg)
                ),
                pose_confidence=pose.confidence,
                reasons=("device reported action failure",),
            )
        else:
            decision = self.gate.evaluate(evidence)
        tracked = TrackedMotionResult(
            evidence=evidence,
            decision=decision,
            pose=pose,
            slip=slip,
            expected_cell=expected_cell,
            expected_heading=expected_heading,
            external_evidence_available=external_distance is not None,
            independent_wall_constraints=walls.independent_axes,
        )
        self.last_result = tracked
        self.last_fusion_input = fusion_input
        return tracked

    def complete_recovery(
        self,
        *,
        recovery_action_id: str,
        original_action: PlannedAction,
        result: Mapping[str, Any],
        original_motion_target: MotionTarget,
        recovery_attempts: int,
    ) -> TrackedMotionResult:
        """Re-evaluate the original grid action from its saved baseline."""
        baseline = self.last_baseline
        if baseline is None:
            raise TaskPoseTrackerError(
                ACTION_NOT_ACTIVE,
                "recovery has no original action baseline",
            )
        if result.get("action_id") != recovery_action_id:
            raise TaskPoseTrackerError(
                ACTION_RESULT_MISMATCH,
                "recovery result action_id does not match recovery action",
            )
        if result.get("name") not in {
            "nudge_forward",
            "align_heading",
        }:
            raise TaskPoseTrackerError(
                ACTION_RESULT_MISMATCH,
                "recovery result name is not a bounded recovery action",
            )
        original_result = {
            **result,
            "action_id": baseline.action_id,
            "name": original_action.name,
        }
        tracked = self.complete_action(
            action_id=baseline.action_id,
            action=original_action,
            result=original_result,
            motion_target=original_motion_target,
        )
        evidence = replace(
            tracked.evidence,
            recovery_attempts=int(recovery_attempts),
        )
        decision = tracked.decision
        if (
            result.get("type") == "done"
            and result.get("success") is not False
        ):
            decision = self.gate.evaluate(evidence)
        tracked = replace(
            tracked,
            evidence=evidence,
            decision=decision,
        )
        self.last_result = tracked
        return tracked

    def accept_action(self, action: PlannedAction) -> PoseEstimate:
        tracked = self.last_result
        if tracked is None or tracked.decision.status != "accepted":
            raise TaskPoseTrackerError(
                ACTION_NOT_ACTIVE,
                "no accepted action evidence is available",
            )
        if self._committed:
            raise TaskPoseTrackerError(
                ACTION_ALREADY_COMMITTED,
                "accepted action was already committed",
            )
        baseline = self.last_baseline
        if baseline is None or baseline.action_name != action.name:
            raise TaskPoseTrackerError(
                ACTION_RESULT_MISMATCH,
                "accepted action does not match active action",
            )
        self._committed = True
        return self.fusion.anchor_grid(
            tracked.expected_cell,
            tracked.expected_heading,
            source="verified_grid_action_done",
        )

    def _require_baseline(
        self,
        action_id: str,
        action: PlannedAction,
    ) -> ActionPoseBaseline:
        baseline = self.last_baseline
        if baseline is None:
            raise TaskPoseTrackerError(
                ACTION_NOT_ACTIVE,
                "action has no saved pose baseline",
            )
        if (
            baseline.action_id != action_id
            or baseline.action_name != action.name
        ):
            raise TaskPoseTrackerError(
                ACTION_RESULT_MISMATCH,
                "active action does not match requested completion",
            )
        return baseline


def _expected_pose(
    maze: MazeMap,
    action: PlannedAction,
) -> tuple[tuple[int, int], str]:
    cell = maze.position
    heading = maze.heading
    if action.name == "move_cell":
        direction = action.direction or heading
        return maze.neighbor(cell, direction), heading.value
    turns = {
        "turn_left": -1,
        "turn_right": 1,
        "turn_back": 2,
    }
    if action.name in turns:
        index = ORDER.index(heading)
        return cell, ORDER[(index + turns[action.name]) % 4].value
    return cell, heading.value
