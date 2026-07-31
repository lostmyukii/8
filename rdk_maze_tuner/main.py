"""Command-line entry point for the RDK X3 maze controller."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rdk_maze_tuner.core.param_manager import ParamManager
from rdk_maze_tuner.core.auto_tuner import AutoTuner
from rdk_maze_tuner.core.logger import JsonlLogger, write_json
from rdk_maze_tuner.core.maze_map import MazeMap
from rdk_maze_tuner.core.maze_planner import MazePlanner
from rdk_maze_tuner.core.maze_runner import MazeRunner
from rdk_maze_tuner.core.motion_analyzer import MotionAnalyzer
from rdk_maze_tuner.core.serial_client import SerialClient, open_serial
from rdk_maze_tuner.core.tcp_stream import open_tcp


DEFAULT_PARAMS = Path(__file__).resolve().parent / "config" / "params.yaml"
DEFAULT_LIMITS = Path(__file__).resolve().parent / "config" / "limits.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RDK X3 maze car controller")
    transport = parser.add_mutually_exclusive_group(required=True)
    transport.add_argument("--serial", help="ESP32 serial port, for example /dev/ttyUSB0 or /dev/ttyACM0")
    transport.add_argument("--tcp", metavar="HOST:PORT", help="Webots simulation endpoint, for example 127.0.0.1:8765")
    parser.add_argument("--baud", type=int, default=115200, help="Serial baud rate")
    parser.add_argument("--timeout", type=float, default=3.0, help="Protocol wait timeout in seconds")
    parser.add_argument("--params", type=Path, default=DEFAULT_PARAMS, help="Path to params.yaml")
    parser.add_argument("--limits", type=Path, default=DEFAULT_LIMITS, help="Path to limits.yaml")
    parser.add_argument(
        "--mode",
        choices=("setup", "action", "explore"),
        default="setup",
        help="Run setup only, one manual action, or automatic DFS exploration",
    )
    parser.add_argument(
        "--action",
        choices=("move_cell", "turn_left", "turn_right", "turn_back"),
        default="move_cell",
        help="One-shot action used when --mode action",
    )
    parser.add_argument("--steps", type=int, default=1, help="Maximum explore steps used when --mode explore")
    parser.add_argument("--no-auto-tune", action="store_true", help="Disable rule-based auto tuning during explore")
    parser.add_argument("--log-file", type=Path, help="Write experiment events as JSONL")
    parser.add_argument("--export-map", type=Path, help="Write final maze map JSON")
    parser.add_argument("--export-params", type=Path, help="Write final parameter snapshot JSON")
    return parser


def action_defaults(action_name: str, params: ParamManager) -> tuple[float, int]:
    if action_name == "move_cell":
        return float(params.get("motor.base_speed")), int(params.get("motion.cell_ticks"))
    if action_name == "turn_back":
        return float(params.get("motor.turn_speed")), int(params.get("motion.turn_180_ticks"))
    return float(params.get("motor.turn_speed")), int(params.get("motion.turn_90_ticks"))


def run(args: argparse.Namespace) -> int:
    params = ParamManager(params_path=args.params, limits_path=args.limits)
    if args.tcp:
        stream = open_tcp(args.tcp)
    else:
        stream = open_serial(args.serial, baud=args.baud)
    client = SerialClient(stream, timeout_s=args.timeout)
    logger = JsonlLogger(args.log_file) if args.log_file else None
    maze = None

    try:
        ready = client.wait_ready()
        print(f"ESP32 ready: {ready.get('fw', 'unknown')} {ready.get('version', '')}".strip())
        if logger is not None:
            logger.record("ready", ready)

        ack = client.send_params(params.esp32_params())
        print(f"parameters sent: ack seq={ack.get('seq')}")
        if logger is not None:
            logger.record("param_snapshot", params.snapshot())
            logger.record("ack", ack)

        if args.mode == "action":
            speed, ticks = action_defaults(args.action, params)
            result = client.execute_action(
                action_id="manual-0001",
                name=args.action,
                speed=speed,
                target_ticks=ticks,
            )
            print(f"action done: {result}")
            if logger is not None:
                logger.record("done", result)

        if args.mode == "explore":
            maze = MazeMap(wall_threshold_mm=int(params.get("tof.wall_threshold_mm")))
            analyzer = MotionAnalyzer(params)
            tuner = None if args.no_auto_tune else AutoTuner(params)
            runner = MazeRunner(
                client=client,
                params=params,
                maze=maze,
                planner=MazePlanner(),
                analyzer=analyzer,
                tuner=tuner,
                logger=logger,
            )
            for index in range(max(0, args.steps)):
                step = runner.run_step()
                print(f"step {index + 1}: action={step.action.name} action_id={step.action_id}")
                if step.motion_report is not None:
                    print(f"motion issues: {','.join(step.motion_report.issues) or 'none'}")
                if step.tune_event is not None and step.tune_event.get("changes"):
                    print(f"auto tune: {step.tune_event['changes']}")
                print(step.map_text)
                if step.action.name == "stop":
                    break
    finally:
        if args.export_map and maze is not None:
            write_json(args.export_map, maze.to_dict())
        if args.export_params:
            write_json(args.export_params, params.snapshot())
        if logger is not None:
            logger.close()
        close = getattr(stream, "close", None)
        if close is not None:
            close()

    return 0


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
