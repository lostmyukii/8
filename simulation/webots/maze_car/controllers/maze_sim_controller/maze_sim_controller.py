"""Webots supervisor that animates the deterministic maze simulation."""

from __future__ import annotations

import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from controller import Supervisor  # type: ignore  # noqa: E402

from simulation.webots.maze_car.controllers.maze_sim_controller.sim_engine import MazeSimEngine  # noqa: E402
from simulation.webots.maze_car.controllers.maze_sim_controller.sim_server import SimProtocolServer  # noqa: E402


def main() -> int:
    robot = Supervisor()
    timestep_ms = int(robot.getBasicTimeStep())
    engine = MazeSimEngine()
    server = SimProtocolServer(
        engine,
        host=os.environ.get("MAZE_SIM_HOST", "127.0.0.1"),
        port=int(os.environ.get("MAZE_SIM_PORT", "8765")),
    )
    translation = robot.getSelf().getField("translation")
    rotation = robot.getSelf().getField("rotation")

    try:
        while robot.step(timestep_ms) != -1:
            now_ms = int(robot.getTime() * 1000)
            server.poll(now_ms=now_ms)
            x, z, yaw = engine.world_pose()
            translation.setSFVec3f([x, 0.075, z])
            rotation.setSFRotation([0.0, 1.0, 0.0, yaw])
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
