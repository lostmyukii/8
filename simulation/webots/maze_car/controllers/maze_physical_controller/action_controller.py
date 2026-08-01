"""Non-blocking ESP32-like action control for the Webots physical car."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from rdk_maze_tuner.core.pose_types import angle_delta_deg
from simulation.webots.maze_car.physical_config import PhysicalProfile

from .motor_model import DualMotorModel, MotorModelOutput
from .physical_types import PhysicalDeviceSample
from .pid import PidConfig, VelocityPid


class ActionRejected(RuntimeError):
    """Raised when an action is unsafe or cannot be started."""


@dataclass(frozen=True)
class ActionRequest:
    action_id: str
    name: str
    target_ticks: int
    speed: float


@dataclass(frozen=True)
class ActionControlConfig:
    default_target_ticks: int = 1350
    position_tolerance_ticks: int = 12
    angle_tolerance_deg: float = 4.0
    settle_speed_rad_s: float = 0.45
    settle_ticks_required: int = 4
    slowdown_ticks: int = 240
    minimum_speed_scale: float = 0.22
    danger_stop_mm: float = 60.0
    action_timeout_ms: int = 8000
    heartbeat_timeout_ms: int = 1200
    stall_timeout_ms: int = 900
    stall_progress_ticks: int = 2
    wheelspin_timeout_ms: int = 600
    wheelspin_predicted_mm: float = 25.0
    wheelspin_external_mm: float = 4.0
    collision_accel_mps2: float = 7.0
    collision_distance_mm: float = 110.0
    collision_speed_rad_s: float = 0.6
    encoder_balance_gain: float = 0.025
    heading_gain: float = 0.03
    pid_kp: float = 0.09
    pid_ki: float = 0.03
    pid_kd: float = 0.001
    pid_integral_limit: float = 4.0

    def __post_init__(self) -> None:
        positive = (
            self.default_target_ticks,
            self.position_tolerance_ticks,
            self.angle_tolerance_deg,
            self.settle_speed_rad_s,
            self.settle_ticks_required,
            self.slowdown_ticks,
            self.danger_stop_mm,
            self.action_timeout_ms,
            self.heartbeat_timeout_ms,
            self.stall_timeout_ms,
            self.stall_progress_ticks,
            self.wheelspin_timeout_ms,
            self.wheelspin_predicted_mm,
            self.wheelspin_external_mm,
            self.collision_accel_mps2,
            self.collision_distance_mm,
            self.collision_speed_rad_s,
            self.pid_integral_limit,
        )
        if any(float(value) <= 0 for value in positive):
            raise ValueError("action control thresholds must be positive")
        if not 0 < self.minimum_speed_scale <= 1:
            raise ValueError(
                "minimum_speed_scale must be between 0 and 1"
            )


@dataclass(frozen=True)
class ActionControlOutput:
    state: str
    pwm_left: float
    pwm_right: float
    target_velocity_left_rad_s: float
    target_velocity_right_rad_s: float
    motor_velocity_left_rad_s: float
    motor_velocity_right_rad_s: float
    motor_torque_left_nm: float
    motor_torque_right_nm: float
    event: dict[str, Any] | None
    telemetry: dict[str, Any]


_ACTIVE_STATES = frozenset(
    {
        "MOVING_CELL",
        "TURNING_LEFT",
        "TURNING_RIGHT",
        "TURNING_BACK",
        "SETTLING",
        "PAUSING",
        "STOPPING",
    }
)
_ACTION_STATES = {
    "move_cell": "MOVING_CELL",
    "turn_left": "TURNING_LEFT",
    "turn_right": "TURNING_RIGHT",
    "turn_back": "TURNING_BACK",
}


class PhysicalActionController:
    def __init__(
        self,
        *,
        profile: PhysicalProfile,
        config: ActionControlConfig | None = None,
    ) -> None:
        self.profile = profile
        self.config = config or ActionControlConfig()
        pid_config = PidConfig(
            kp=self.config.pid_kp,
            ki=self.config.pid_ki,
            kd=self.config.pid_kd,
            integral_limit=self.config.pid_integral_limit,
            output_limit=1.0,
        )
        self._left_pid = VelocityPid(pid_config)
        self._right_pid = VelocityPid(pid_config)
        self._motor_model = DualMotorModel(profile.motor)
        self.state = "IDLE"
        self._active: ActionRequest | None = None
        self._start_ms = 0
        self._start_encoders = (0, 0)
        self._start_yaw_deg = 0.0
        self._start_front_mm = 0.0
        self._last_heartbeat_ms = 0
        self._last_progress_ticks = 0.0
        self._last_progress_ms = 0
        self._settled_ticks = 0

    def reset(self) -> None:
        self._left_pid.reset()
        self._right_pid.reset()
        self._motor_model.reset()
        self.state = "IDLE"
        self._active = None
        self._settled_ticks = 0

    def heartbeat(self, *, now_ms: int) -> None:
        self._last_heartbeat_ms = int(now_ms)

    def start(
        self,
        request: ActionRequest,
        *,
        sample: PhysicalDeviceSample,
        now_ms: int,
    ) -> None:
        if self.state == "ESTOP":
            raise ActionRejected("estop is latched")
        if self.state != "IDLE" or self._active is not None:
            raise ActionRejected("an action is already active")
        if request.name not in _ACTION_STATES:
            raise ActionRejected(f"unsupported action: {request.name}")
        if not request.action_id.strip():
            raise ActionRejected("action_id is required")
        if not math.isfinite(float(request.speed)) or request.speed <= 0:
            raise ActionRejected("action speed must be finite and positive")
        target_ticks = (
            int(request.target_ticks)
            if int(request.target_ticks) > 0
            else self.config.default_target_ticks
        )
        self._active = ActionRequest(
            action_id=request.action_id,
            name=request.name,
            target_ticks=target_ticks,
            speed=float(request.speed),
        )
        self.state = _ACTION_STATES[request.name]
        self._start_ms = int(now_ms)
        self._start_encoders = (sample.enc_left, sample.enc_right)
        self._start_yaw_deg = sample.imu_yaw_deg
        self._start_front_mm = sample.front_mm
        self._last_heartbeat_ms = int(now_ms)
        self._last_progress_ticks = 0.0
        self._last_progress_ms = int(now_ms)
        self._settled_ticks = 0
        self._left_pid.reset()
        self._right_pid.reset()
        self._motor_model.reset()

    def pause(self) -> None:
        if self._active is not None and self.state in _ACTIVE_STATES:
            self.state = "PAUSING"

    def stop(self) -> None:
        if self._active is not None and self.state in _ACTIVE_STATES:
            self.state = "STOPPING"

    def estop(self, *, now_ms: int) -> ActionControlOutput:
        event = self._error_event(
            code="ESTOP",
            message="emergency stop latched",
            now_ms=now_ms,
        )
        self.state = "ESTOP"
        self._active = None
        self._zero_control()
        return self._zero_output(event=event)

    def clear_estop(self) -> bool:
        if self.state != "ESTOP":
            return False
        self.reset()
        return True

    def tick(
        self,
        *,
        sample: PhysicalDeviceSample,
        now_ms: int,
    ) -> ActionControlOutput:
        now = int(now_ms)
        if self.state == "ESTOP":
            return self._zero_output(event=None)
        if self._active is None:
            return self._zero_output(event=None)
        if self.state in {"PAUSING", "STOPPING"}:
            return self._tick_cancellation(sample=sample, now_ms=now)

        failure = self._safety_failure(sample=sample, now_ms=now)
        if failure is not None:
            return self._fail(
                code=failure[0],
                message=failure[1],
                now_ms=now,
            )

        progress, left_delta, right_delta = self._progress(sample)
        remaining = max(0.0, self._active.target_ticks - progress)
        heading_error = self._heading_error(sample)

        if self.state == "SETTLING":
            return self._tick_settling(
                sample=sample,
                progress=progress,
                heading_error=heading_error,
                now_ms=now,
            )

        if (
            remaining <= self.config.position_tolerance_ticks
            and self._angle_converged(heading_error)
        ):
            self.state = "SETTLING"
            self._settled_ticks = 0
            return self._zero_output(
                event=None,
                telemetry=self._telemetry(
                    progress=progress,
                    remaining=remaining,
                    left_delta=left_delta,
                    right_delta=right_delta,
                    heading_error=heading_error,
                ),
            )

        left_target, right_target = self._wheel_targets(
            progress=progress,
            remaining=remaining,
            left_delta=left_delta,
            right_delta=right_delta,
            heading_error=heading_error,
        )
        return self._closed_loop_output(
            sample=sample,
            left_target=left_target,
            right_target=right_target,
            telemetry=self._telemetry(
                progress=progress,
                remaining=remaining,
                left_delta=left_delta,
                right_delta=right_delta,
                heading_error=heading_error,
            ),
        )

    def _safety_failure(
        self,
        *,
        sample: PhysicalDeviceSample,
        now_ms: int,
    ) -> tuple[str, str] | None:
        assert self._active is not None
        if (
            self._active.name == "move_cell"
            and sample.front_mm < self.config.danger_stop_mm
        ):
            return (
                "OBSTACLE_TOO_CLOSE",
                "front distance is below the danger threshold",
            )
        if now_ms - self._start_ms > self.config.action_timeout_ms:
            return ("ACTION_TIMEOUT", "action exceeded its timeout")
        if (
            now_ms - self._last_heartbeat_ms
            > self.config.heartbeat_timeout_ms
        ):
            return (
                "HEARTBEAT_TIMEOUT",
                "controller heartbeat expired",
            )
        if (
            self._active.name == "move_cell"
            and abs(sample.accel_forward_mps2)
            >= self.config.collision_accel_mps2
            and sample.front_mm <= self.config.collision_distance_mm
            and max(
                abs(sample.wheel_speed_left_rad_s),
                abs(sample.wheel_speed_right_rad_s),
            )
            <= self.config.collision_speed_rad_s
        ):
            return (
                "COLLISION_SUSPECTED",
                "acceleration, range, and wheel-speed evidence imply collision",
            )

        progress, _left_delta, _right_delta = self._progress(sample)
        if (
            progress
            >= self._last_progress_ticks
            + self.config.stall_progress_ticks
        ):
            self._last_progress_ticks = progress
            self._last_progress_ms = now_ms
        elif now_ms - self._last_progress_ms >= self.config.stall_timeout_ms:
            return ("MOTOR_STALL", "encoder progress stalled")

        predicted_mm = (
            progress
            * (2.0 * math.pi * self.profile.geometry.wheel_radius_m)
            * 1000.0
            / self.profile.encoder.ticks_per_revolution
        )
        external_mm = abs(self._start_front_mm - sample.front_mm)
        if (
            self._active.name == "move_cell"
            and predicted_mm >= self.config.wheelspin_predicted_mm
            and external_mm <= self.config.wheelspin_external_mm
            and now_ms - self._start_ms
            >= self.config.wheelspin_timeout_ms
        ):
            return (
                "WHEELSPIN_PERSISTENT",
                "encoders moved without matching IMU/range displacement",
            )
        return None

    def _tick_settling(
        self,
        *,
        sample: PhysicalDeviceSample,
        progress: float,
        heading_error: float,
        now_ms: int,
    ) -> ActionControlOutput:
        assert self._active is not None
        remaining = max(0.0, self._active.target_ticks - progress)
        low_speed = max(
            abs(sample.wheel_speed_left_rad_s),
            abs(sample.wheel_speed_right_rad_s),
        ) <= self.config.settle_speed_rad_s
        if (
            remaining <= self.config.position_tolerance_ticks
            and self._angle_converged(heading_error)
            and low_speed
        ):
            self._settled_ticks += 1
        else:
            self._settled_ticks = 0
        if self._settled_ticks >= self.config.settle_ticks_required:
            return self._complete(sample=sample, now_ms=now_ms)
        return self._zero_output(
            event=None,
            telemetry=self._telemetry(
                progress=progress,
                remaining=remaining,
                left_delta=sample.enc_left - self._start_encoders[0],
                right_delta=sample.enc_right - self._start_encoders[1],
                heading_error=heading_error,
            ),
        )

    def _tick_cancellation(
        self,
        *,
        sample: PhysicalDeviceSample,
        now_ms: int,
    ) -> ActionControlOutput:
        code = "PAUSED" if self.state == "PAUSING" else "STOPPED"
        low_speed = max(
            abs(sample.wheel_speed_left_rad_s),
            abs(sample.wheel_speed_right_rad_s),
        ) <= self.config.settle_speed_rad_s
        if low_speed:
            return self._cancelled(code=code, sample=sample, now_ms=now_ms)
        return self._closed_loop_output(
            sample=sample,
            left_target=0.0,
            right_target=0.0,
            telemetry={"cancel_pending": code.lower()},
        )

    def _wheel_targets(
        self,
        *,
        progress: float,
        remaining: float,
        left_delta: int,
        right_delta: int,
        heading_error: float,
    ) -> tuple[float, float]:
        assert self._active is not None
        speed_fraction = min(1.0, abs(self._active.speed))
        base = speed_fraction * self.profile.motor.max_velocity_rad_s
        scale = max(
            self.config.minimum_speed_scale,
            min(1.0, remaining / self.config.slowdown_ticks),
        )
        base *= scale
        if self._active.name == "move_cell":
            encoder_error = left_delta - right_delta
            correction = (
                encoder_error * self.config.encoder_balance_gain
                - heading_error * self.config.heading_gain
            )
            return (base - correction, base + correction)
        if self._active.name == "turn_left":
            return (-base, base)
        return (base, -base)

    def _closed_loop_output(
        self,
        *,
        sample: PhysicalDeviceSample,
        left_target: float,
        right_target: float,
        telemetry: dict[str, Any],
    ) -> ActionControlOutput:
        dt_s = sample.controller_period_ms / 1000.0
        left_pid = self._left_pid.update(
            setpoint=left_target,
            measurement=sample.wheel_speed_left_rad_s,
            dt_s=dt_s,
        )
        right_pid = self._right_pid.update(
            setpoint=right_target,
            measurement=sample.wheel_speed_right_rad_s,
            dt_s=dt_s,
        )
        motor = self._motor_model.step(
            pwm_left=left_pid.output,
            pwm_right=right_pid.output,
            dt_s=dt_s,
        )
        return self._output(
            motor=motor,
            left_target=left_target,
            right_target=right_target,
            event=None,
            telemetry={
                **telemetry,
                "pid_left": left_pid.output,
                "pid_right": right_pid.output,
            },
        )

    def _complete(
        self,
        *,
        sample: PhysicalDeviceSample,
        now_ms: int,
    ) -> ActionControlOutput:
        assert self._active is not None
        event = {
            "type": "done",
            "action_id": self._active.action_id,
            "name": self._active.name,
            "success": True,
            "duration_ms": now_ms - self._start_ms,
            "enc_left": sample.enc_left,
            "enc_right": sample.enc_right,
            "target_ticks": self._active.target_ticks,
        }
        self.state = "IDLE"
        self._active = None
        self._zero_control()
        return self._zero_output(event=event)

    def _fail(
        self,
        *,
        code: str,
        message: str,
        now_ms: int,
    ) -> ActionControlOutput:
        event = self._error_event(
            code=code,
            message=message,
            now_ms=now_ms,
        )
        self.state = "ERROR"
        self._active = None
        self._zero_control()
        return self._zero_output(event=event)

    def _cancelled(
        self,
        *,
        code: str,
        sample: PhysicalDeviceSample,
        now_ms: int,
    ) -> ActionControlOutput:
        event = self._error_event(
            code=code,
            message=f"action ended by {code.lower()} request",
            now_ms=now_ms,
            sample=sample,
        )
        self.state = "IDLE"
        self._active = None
        self._zero_control()
        return self._zero_output(event=event)

    def _error_event(
        self,
        *,
        code: str,
        message: str,
        now_ms: int,
        sample: PhysicalDeviceSample | None = None,
    ) -> dict[str, Any]:
        active = self._active
        return {
            "type": "error",
            "action_id": active.action_id if active else None,
            "name": active.name if active else None,
            "code": code,
            "message": message,
            "duration_ms": max(0, int(now_ms) - self._start_ms),
            "enc_left": sample.enc_left if sample else None,
            "enc_right": sample.enc_right if sample else None,
        }

    def _progress(
        self,
        sample: PhysicalDeviceSample,
    ) -> tuple[float, int, int]:
        left_delta = sample.enc_left - self._start_encoders[0]
        right_delta = sample.enc_right - self._start_encoders[1]
        if self._active is not None and self._active.name == "move_cell":
            progress = (left_delta + right_delta) / 2.0
        else:
            progress = (abs(left_delta) + abs(right_delta)) / 2.0
        return max(0.0, progress), left_delta, right_delta

    def _heading_error(
        self,
        sample: PhysicalDeviceSample,
    ) -> float:
        if self._active is None or not sample.imu_available:
            return 0.0
        delta = {
            "move_cell": 0.0,
            "turn_left": -90.0,
            "turn_right": 90.0,
            "turn_back": 180.0,
        }[self._active.name]
        target = (self._start_yaw_deg + delta) % 360.0
        return angle_delta_deg(target, sample.imu_yaw_deg)

    def _angle_converged(self, heading_error: float) -> bool:
        if self._active is None:
            return True
        if self._active.name == "move_cell":
            return True
        return abs(heading_error) <= self.config.angle_tolerance_deg

    def _telemetry(
        self,
        *,
        progress: float,
        remaining: float,
        left_delta: int,
        right_delta: int,
        heading_error: float,
    ) -> dict[str, Any]:
        return {
            "action_id": self._active.action_id if self._active else None,
            "progress_ticks": round(progress, 3),
            "remaining_ticks": round(remaining, 3),
            "encoder_balance_error_ticks": left_delta - right_delta,
            "heading_error_deg": round(heading_error, 6),
            "settled_ticks": self._settled_ticks,
        }

    def _zero_control(self) -> None:
        self._left_pid.reset()
        self._right_pid.reset()
        self._motor_model.reset()

    def _zero_output(
        self,
        *,
        event: dict[str, Any] | None,
        telemetry: dict[str, Any] | None = None,
    ) -> ActionControlOutput:
        return ActionControlOutput(
            state=self.state,
            pwm_left=0.0,
            pwm_right=0.0,
            target_velocity_left_rad_s=0.0,
            target_velocity_right_rad_s=0.0,
            motor_velocity_left_rad_s=0.0,
            motor_velocity_right_rad_s=0.0,
            motor_torque_left_nm=0.0,
            motor_torque_right_nm=0.0,
            event=event,
            telemetry=dict(telemetry or {}),
        )

    def _output(
        self,
        *,
        motor: MotorModelOutput,
        left_target: float,
        right_target: float,
        event: dict[str, Any] | None,
        telemetry: dict[str, Any],
    ) -> ActionControlOutput:
        return ActionControlOutput(
            state=self.state,
            pwm_left=motor.pwm_left,
            pwm_right=motor.pwm_right,
            target_velocity_left_rad_s=left_target,
            target_velocity_right_rad_s=right_target,
            motor_velocity_left_rad_s=motor.left_velocity_rad_s,
            motor_velocity_right_rad_s=motor.right_velocity_rad_s,
            motor_torque_left_nm=motor.left_torque_nm,
            motor_torque_right_nm=motor.right_torque_nm,
            event=event,
            telemetry=telemetry,
        )
