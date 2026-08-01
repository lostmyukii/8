"""Read-only P1 stability observer for the physical Webots maze car.

The controller deliberately does not command either wheel during P1.  Its
Supervisor capability is reserved for reset/map orchestration and ground-truth
observation in later tasks; movement must always come from wheel motors.
"""

from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from controller import Supervisor


@dataclass
class StabilitySample:
    position: tuple[float, float, float]
    orientation: tuple[float, ...]
    velocity: tuple[float, ...]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _rotation_delta_deg(a: Sequence[float], b: Sequence[float]) -> float:
    # Relative rotation trace: trace(A^T B).
    trace = sum(a[row * 3 + col] * b[row * 3 + col] for row in range(3) for col in range(3))
    cosine = _clamp((trace - 1.0) * 0.5, -1.0, 1.0)
    return math.degrees(math.acos(cosine))


def _tilt_deg(orientation: Sequence[float]) -> float:
    # The local +Y axis is the second column of Webots' 3x3 orientation matrix.
    up_alignment = _clamp(orientation[4], -1.0, 1.0)
    return math.degrees(math.acos(up_alignment))


def _sample(node) -> StabilitySample:
    return StabilitySample(
        position=tuple(float(value) for value in node.getPosition()),
        orientation=tuple(float(value) for value in node.getOrientation()),
        velocity=tuple(float(value) for value in node.getVelocity()),
    )


def main() -> None:
    robot = Supervisor()
    time_step_ms = int(robot.getBasicTimeStep())
    self_node = robot.getSelf()
    if self_node is None:
        print("MAZE_P1_ERROR " + json.dumps({"code": "SELF_NODE_MISSING"}), flush=True)
        robot.simulationQuit(1)
        return

    stability_mode = (
        os.environ.get("MAZE_P1_STABILITY", "") == "1"
        or "--p1-stability" in sys.argv[1:]
    )
    duration_argument = next(
        (
            argument.split("=", 1)[1]
            for argument in sys.argv[1:]
            if argument.startswith("--p1-duration=")
        ),
        None,
    )
    duration_s = float(
        duration_argument or os.environ.get("MAZE_P1_DURATION_S", "10")
    )
    report_argument = next(
        (
            argument.split("=", 1)[1]
            for argument in sys.argv[1:]
            if argument.startswith("--p1-report=")
        ),
        None,
    )
    report_path = Path(
        report_argument
        or os.environ.get("MAZE_P1_REPORT_PATH", "/tmp/maze-p1-stability.json")
    )
    initial = _sample(self_node)
    latest = initial
    max_vertical_speed = 0.0
    max_linear_speed = 0.0
    max_tilt = _tilt_deg(initial.orientation)
    elapsed_s = 0.0

    while robot.step(time_step_ms) != -1:
        latest = _sample(self_node)
        max_vertical_speed = max(max_vertical_speed, abs(latest.velocity[1]))
        max_linear_speed = max(
            max_linear_speed,
            math.sqrt(sum(component * component for component in latest.velocity[:3])),
        )
        max_tilt = max(max_tilt, _tilt_deg(latest.orientation))
        elapsed_s += time_step_ms / 1000.0

        if stability_mode and elapsed_s >= duration_s:
            displacement = [
                latest.position[index] - initial.position[index] for index in range(3)
            ]
            report = {
                "duration_s": round(elapsed_s, 3),
                "initial_position_m": [round(value, 9) for value in initial.position],
                "final_position_m": [round(value, 9) for value in latest.position],
                "displacement_m": [round(value, 9) for value in displacement],
                "horizontal_drift_m": round(math.hypot(displacement[0], displacement[2]), 9),
                "vertical_drift_m": round(displacement[1], 9),
                "orientation_delta_deg": round(
                    _rotation_delta_deg(initial.orientation, latest.orientation), 6
                ),
                "max_vertical_speed_mps": round(max_vertical_speed, 9),
                "max_linear_speed_mps": round(max_linear_speed, 9),
                "max_tilt_deg": round(max_tilt, 6),
                "fell_over": max_tilt >= 60.0,
                "below_floor": latest.position[1] < -0.02,
            }
            temporary_report = report_path.with_suffix(report_path.suffix + ".tmp")
            temporary_report.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary_report.replace(report_path)
            print("MAZE_P1_STABILITY " + json.dumps(report, sort_keys=True), flush=True)
            robot.simulationQuit(0)
            return


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "MAZE_P1_ERROR "
            + json.dumps(
                {"code": "CONTROLLER_EXCEPTION", "detail": str(exc)},
                sort_keys=True,
            ),
            flush=True,
        )
        sys.exit(1)
