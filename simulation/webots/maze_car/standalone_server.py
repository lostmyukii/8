"""Run the simulated ESP32 protocol without launching Webots."""

from __future__ import annotations

import argparse
import time

from simulation.webots.maze_car.controllers.maze_sim_controller.sim_engine import MazeSimEngine
from simulation.webots.maze_car.controllers.maze_sim_controller.sim_server import SimProtocolServer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Maze-car simulated ESP32 TCP server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    engine = MazeSimEngine()
    server = SimProtocolServer(engine, host=args.host, port=args.port)
    started = time.monotonic()
    try:
        while True:
            now_ms = int((time.monotonic() - started) * 1000)
            server.poll(now_ms=now_ms)
            time.sleep(0.01)
    except KeyboardInterrupt:
        return 0
    finally:
        server.close()


if __name__ == "__main__":
    raise SystemExit(main())
