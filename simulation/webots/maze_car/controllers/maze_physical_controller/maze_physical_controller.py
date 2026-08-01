"""Webots entrypoint for P1 observation and the physical protocol engine."""

from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from controller import Supervisor


PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from simulation.webots.maze_car.controllers.maze_physical_controller.physical_devices import (  # noqa: E402
    PhysicalDeviceAdapter,
)
from simulation.webots.maze_car.controllers.maze_physical_controller.physical_engine import (  # noqa: E402
    PhysicalMazeEngine,
)
from simulation.webots.maze_car.controllers.maze_physical_controller.physical_world import (  # noqa: E402
    PhysicalWorldConfigurator,
)
from simulation.webots.maze_car.controllers.maze_physical_controller.truth_observer import (  # noqa: E402
    TruthObserver,
)
from simulation.webots.maze_car.controllers.maze_sim_controller.sim_server import (  # noqa: E402
    SimProtocolServer,
)
from simulation.webots.maze_car.map_loader import (  # noqa: E402
    WebotsMapLoader,
    calibration_map_definition,
    default_map_definition,
)
from simulation.webots.maze_car.physical_config import (  # noqa: E402
    PhysicalProfileRepository,
)


@dataclass
class StabilitySample:
    position: tuple[float, float, float]
    orientation: tuple[float, ...]
    velocity: tuple[float, ...]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _rotation_delta_deg(
    a: Sequence[float],
    b: Sequence[float],
) -> float:
    trace = sum(
        a[row * 3 + col] * b[row * 3 + col]
        for row in range(3)
        for col in range(3)
    )
    cosine = _clamp((trace - 1.0) * 0.5, -1.0, 1.0)
    return math.degrees(math.acos(cosine))


def _tilt_deg(orientation: Sequence[float]) -> float:
    up_alignment = _clamp(orientation[4], -1.0, 1.0)
    return math.degrees(math.acos(up_alignment))


def _stability_sample(node) -> StabilitySample:
    return StabilitySample(
        position=tuple(float(value) for value in node.getPosition()),
        orientation=tuple(
            float(value) for value in node.getOrientation()
        ),
        velocity=tuple(float(value) for value in node.getVelocity()),
    )


def _argument_value(prefix: str) -> str | None:
    return next(
        (
            argument.split("=", 1)[1]
            for argument in sys.argv[1:]
            if argument.startswith(prefix + "=")
        ),
        None,
    )


def _run_stability(robot: Supervisor) -> None:
    time_step_ms = int(robot.getBasicTimeStep())
    self_node = robot.getSelf()
    if self_node is None:
        print(
            "MAZE_P1_ERROR "
            + json.dumps({"code": "SELF_NODE_MISSING"}),
            flush=True,
        )
        robot.simulationQuit(1)
        return

    duration_s = float(
        _argument_value("--p1-duration")
        or os.environ.get("MAZE_P1_DURATION_S", "10")
    )
    report_path = Path(
        _argument_value("--p1-report")
        or os.environ.get(
            "MAZE_P1_REPORT_PATH",
            "/tmp/maze-p1-stability.json",
        )
    )
    initial = _stability_sample(self_node)
    latest = initial
    max_vertical_speed = 0.0
    max_linear_speed = 0.0
    max_tilt = _tilt_deg(initial.orientation)
    elapsed_s = 0.0

    while robot.step(time_step_ms) != -1:
        latest = _stability_sample(self_node)
        max_vertical_speed = max(
            max_vertical_speed,
            abs(latest.velocity[1]),
        )
        max_linear_speed = max(
            max_linear_speed,
            math.sqrt(
                sum(
                    component * component
                    for component in latest.velocity[:3]
                )
            ),
        )
        max_tilt = max(max_tilt, _tilt_deg(latest.orientation))
        elapsed_s += time_step_ms / 1000.0
        if elapsed_s < duration_s:
            continue

        displacement = [
            latest.position[index] - initial.position[index]
            for index in range(3)
        ]
        report = {
            "duration_s": round(elapsed_s, 3),
            "initial_position_m": [
                round(value, 9) for value in initial.position
            ],
            "final_position_m": [
                round(value, 9) for value in latest.position
            ],
            "displacement_m": [
                round(value, 9) for value in displacement
            ],
            "horizontal_drift_m": round(
                math.hypot(displacement[0], displacement[2]),
                9,
            ),
            "vertical_drift_m": round(displacement[1], 9),
            "orientation_delta_deg": round(
                _rotation_delta_deg(
                    initial.orientation,
                    latest.orientation,
                ),
                6,
            ),
            "max_vertical_speed_mps": round(
                max_vertical_speed,
                9,
            ),
            "max_linear_speed_mps": round(max_linear_speed, 9),
            "max_tilt_deg": round(max_tilt, 6),
            "fell_over": max_tilt >= 60.0,
            "below_floor": latest.position[1] < -0.02,
        }
        temporary_report = report_path.with_suffix(
            report_path.suffix + ".tmp"
        )
        temporary_report.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary_report.replace(report_path)
        print(
            "MAZE_P1_STABILITY "
            + json.dumps(report, sort_keys=True),
            flush=True,
        )
        robot.simulationQuit(0)
        return


def _run_physical_protocol(robot: Supervisor) -> None:
    time_step_ms = int(robot.getBasicTimeStep())
    profiles = PhysicalProfileRepository()
    profile_id = os.environ.get(
        "MAZE_PHYSICAL_PROFILE_ID",
        os.environ.get(
            "MAZE_DEFAULT_PHYSICAL_PROFILE",
            "normal-v1",
        ),
    )
    expected_digest = os.environ.get(
        "MAZE_PHYSICAL_PROFILE_DIGEST"
    )
    profile = profiles.get(
        profile_id,
        expected_digest=expected_digest,
    )
    world = PhysicalWorldConfigurator(
        robot,
        map_loader=WebotsMapLoader(robot),
        settle_steps=100,
        basic_time_step_ms=time_step_ms,
    )
    ideal_sensor_mode = (
        os.environ.get("MAZE_PHYSICAL_SENSOR_MODE", "").lower()
        == "ideal"
    )
    world.configure_sensor_mode(ideal=ideal_sensor_mode)
    device = PhysicalDeviceAdapter(
        robot,
        profile,
        sensor_noise_enabled=not ideal_sensor_mode,
        sensor_dropout_enabled=not ideal_sensor_mode,
    )
    if robot.step(time_step_ms) == -1:
        raise RuntimeError(
            "Webots stopped before physical devices produced a sample"
        )
    self_node = robot.getSelf()
    if self_node is None:
        raise RuntimeError("physical robot Supervisor node is missing")
    calibration_mode = "--world=calibration" in sys.argv[1:]
    map_definition = (
        calibration_map_definition()
        if calibration_mode
        else default_map_definition()
    )
    engine = PhysicalMazeEngine(
        device_adapter=device,
        world=world,
        truth_observer=TruthObserver(self_node),
        profile_repository=profiles,
        profile_id=profile.profile_id,
        map_definition=map_definition,
        map_version_id=(
            "builtin-calibration-3x3"
            if calibration_mode
            else "builtin-open-5x5"
        ),
    )
    engine.handle({"type": "reset", "seq": 0}, now_ms=0)
    acceptance_screenshot = os.environ.get(
        "MAZE_ACCEPTANCE_SCREENSHOT_PATH"
    )
    if acceptance_screenshot:
        robot.exportImage(acceptance_screenshot, 95)
        print(
            "MAZE_PHYSICAL_SCREENSHOT "
            + json.dumps(
                {"path": acceptance_screenshot},
                sort_keys=True,
            ),
            flush=True,
        )
    server = SimProtocolServer(
        engine,
        host=os.environ.get("MAZE_SIM_HOST", "127.0.0.1"),
        port=int(os.environ.get("MAZE_SIM_PORT", "8765")),
    )
    print(
        "MAZE_PHYSICAL_READY "
        + json.dumps(
            {
                "profile_id": profile.profile_id,
                "profile_digest": profile.digest,
                "map_digest": engine.map_digest,
                "period_ms": time_step_ms,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        while robot.step(time_step_ms) != -1:
            server.poll(now_ms=int(round(robot.getTime() * 1000.0)))
    finally:
        server.close()


def main() -> None:
    robot = Supervisor()
    stability_mode = (
        os.environ.get("MAZE_P1_STABILITY", "") == "1"
        or "--p1-stability" in sys.argv[1:]
    )
    if stability_mode:
        _run_stability(robot)
        return
    _run_physical_protocol(robot)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "MAZE_PHYSICAL_ERROR "
            + json.dumps(
                {
                    "code": "CONTROLLER_EXCEPTION",
                    "detail": str(exc),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        sys.exit(1)
