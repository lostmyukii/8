"""Run isolated P1-P4 Webots acceptance and archive raw evidence."""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from rdk_maze_tuner.core.device_session import DeviceSession
from rdk_maze_tuner.core.serial_client import SerialClient
from rdk_maze_tuner.core.tcp_stream import open_tcp
from simulation.webots.maze_car.physical_config import (
    DEFAULT_PHYSICAL_PROFILE_DIR,
    PhysicalProfile,
    PhysicalProfileRepository,
)
from simulation.webots.maze_car.physical_scenarios import (
    PhysicalScenario,
    PhysicalScenarioRepository,
    evaluate_scenario_frames,
)

from .physical_acceptance_schema import (
    AcceptanceReportError,
    validate_acceptance_report,
)


PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_WORLD = (
    PROJECT_ROOT
    / "simulation/webots/maze_car/worlds/"
    "maze_physical_calibration.wbt"
)
DEFAULT_SCENARIOS = (
    PROJECT_ROOT
    / "simulation/webots/maze_car/config/"
    "acceptance_scenarios.yaml"
)


@dataclass(frozen=True)
class AcceptanceRunConfig:
    webots: Path
    world: Path
    scenarios: Path
    output: Path
    total_timeout_s: float = 600.0


class ManagedWebotsProcess:
    """Own exactly one launched Webots process and its PID record."""

    def __init__(
        self,
        *,
        command: list[str],
        environment: Mapping[str, str],
        pid_file: Path,
        popen: Callable[..., Any] = subprocess.Popen,
        cwd: Path | None = None,
        stdout_path: Path | None = None,
    ) -> None:
        self.command = list(command)
        self.environment = dict(environment)
        self.pid_file = Path(pid_file)
        self.popen = popen
        self.cwd = None if cwd is None else Path(cwd)
        self.stdout_path = (
            None if stdout_path is None else Path(stdout_path)
        )
        self.process: Any = None
        self._stdout = None

    def __enter__(self):
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {
            "env": {**os.environ, **self.environment},
            "cwd": None if self.cwd is None else str(self.cwd),
            "start_new_session": True,
            "text": True,
        }
        if self.stdout_path is not None:
            self.stdout_path.parent.mkdir(parents=True, exist_ok=True)
            self._stdout = self.stdout_path.open(
                "w",
                encoding="utf-8",
            )
            kwargs.update(
                {
                    "stdout": self._stdout,
                    "stderr": subprocess.STDOUT,
                }
            )
        self.process = self.popen(self.command, **kwargs)
        self.pid_file.write_text(
            f"{int(self.process.pid)}\n",
            encoding="utf-8",
        )
        return self.process

    def __exit__(self, *_exc_info: object) -> None:
        process = self.process
        try:
            if process is not None and process.poll() is None:
                if isinstance(process, subprocess.Popen):
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
                try:
                    process.wait(timeout=8.0)
                except subprocess.TimeoutExpired:
                    if isinstance(process, subprocess.Popen):
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    process.wait(timeout=3.0)
        finally:
            self.pid_file.unlink(missing_ok=True)
            if self._stdout is not None:
                self._stdout.close()


class PhysicalAcceptanceRunner:
    def __init__(
        self,
        config: AcceptanceRunConfig,
        *,
        reserve_port: Callable[[], int] | None = None,
        scenario_executor: Callable[..., dict[str, Any]] | None = None,
        stability_executor: Callable[..., dict[str, Any]] | None = None,
        webots_version: Callable[[Path], str] | None = None,
        source_commit: Callable[[], str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.reserve_port = reserve_port or reserve_loopback_port
        self.scenario_executor = (
            scenario_executor or self._execute_scenario
        )
        self.stability_executor = (
            stability_executor or self._execute_stability
        )
        self.webots_version = (
            webots_version or detect_webots_version
        )
        self.source_commit = source_commit or detect_source_commit
        self.monotonic = monotonic
        self.utc_now = utc_now or (lambda: datetime.now(UTC))

    def run(self) -> dict[str, Any]:
        started = self.utc_now()
        run_id = (
            "physical-"
            + started.strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + uuid.uuid4().hex[:8]
        )
        output_root = self.config.output.resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        temporary_dir = output_root / f".tmp-{run_id}"
        final_dir = output_root / run_id
        temporary_dir.mkdir(parents=False, exist_ok=False)
        events_path = temporary_dir / "events.jsonl"
        deadline = self.monotonic() + float(
            self.config.total_timeout_s
        )

        def event_sink(event_type: str, payload: Mapping[str, Any]) -> None:
            append_jsonl(
                events_path,
                {
                    "utc_timestamp": utc_text(self.utc_now()),
                    "monotonic_s": round(self.monotonic(), 6),
                    "type": str(event_type),
                    "payload": dict(payload),
                },
            )

        base = {
            "schema_version": 1,
            "run_id": run_id,
            "status": "FAIL",
            "source_commit": safe_call(
                self.source_commit,
                fallback="unknown",
            ),
            "webots_version": "unavailable",
            "started_at_utc": utc_text(started),
            "ended_at_utc": utc_text(started),
            "output_dir": run_id,
            "profiles": [],
            "maps": [],
            "p1": unavailable_stage(),
            "p2": unavailable_stage(),
            "scenarios": [],
            "performance": {
                "real_time_factor": None,
                "controller_period_ms": None,
            },
            "errors": [],
            "artifacts": {
                "events_jsonl": "events.jsonl",
                "report_json": "report.json",
            },
        }
        report = dict(base)
        try:
            if not executable_file(self.config.webots):
                report["status"] = "unavailable"
                report["errors"] = [
                    {
                        "code": "WEBOTS_UNAVAILABLE",
                        "message": (
                            "Webots executable is missing or not executable: "
                            f"{self.config.webots}"
                        ),
                    }
                ]
                event_sink("unavailable", report["errors"][0])
                return self._finalize(
                    report,
                    temporary_dir,
                    final_dir,
                )

            report["webots_version"] = self.webots_version(
                self.config.webots
            )
            repository = PhysicalScenarioRepository(
                self.config.scenarios
            )
            profiles = PhysicalProfileRepository(
                DEFAULT_PHYSICAL_PROFILE_DIR
            )
            scenarios = tuple(
                sorted(
                    repository.list_scenarios(),
                    key=lambda scenario: (
                        scenario.physical_profile_id != "normal-v1",
                        scenario.scenario_id,
                    ),
                )
            )
            report["profiles"] = unique_profiles(
                scenarios,
                profiles=profiles,
            )
            report["maps"] = unique_maps(scenarios)
            self._ensure_time(deadline)
            p1_dir = temporary_dir / ".tmp-p1"
            p1_dir.mkdir()
            report["p1"] = self.stability_executor(
                port=0,
                work_dir=p1_dir,
                event_sink=event_sink,
                deadline=deadline,
            )

            normal_initial_frames: list[dict[str, Any]] = []
            scenario_reports: list[dict[str, Any]] = []
            for scenario in scenarios:
                self._ensure_time(deadline)
                work_dir = (
                    temporary_dir / f".tmp-{scenario.scenario_id}"
                )
                work_dir.mkdir()
                port = self.reserve_port()
                event_sink(
                    "scenario.start",
                    {
                        "scenario_id": scenario.scenario_id,
                        "port": port,
                        "work_dir": work_dir.name,
                    },
                )
                result = self.scenario_executor(
                    scenario,
                    port=port,
                    work_dir=work_dir,
                    event_sink=event_sink,
                )
                initial_frames = result.pop("initial_frames", [])
                if scenario.physical_profile_id == "normal-v1":
                    normal_initial_frames = [
                        dict(item) for item in initial_frames
                    ]
                scenario_reports.append(result)
                report["scenarios"] = scenario_reports
                event_sink(
                    "scenario.complete",
                    {
                        "scenario_id": scenario.scenario_id,
                        "status": result.get("status"),
                    },
                )
            report["scenarios"] = scenario_reports
            report["p2"] = evaluate_p2(normal_initial_frames)
            report["performance"] = aggregate_performance(
                scenario_reports
            )
            all_pass = (
                report["p1"].get("status") == "PASS"
                and report["p2"].get("status") == "PASS"
                and scenario_reports
                and all(
                    item.get("status") == "PASS"
                    for item in scenario_reports
                )
            )
            report["status"] = "PASS" if all_pass else "FAIL"
        except Exception as exc:
            report["status"] = "FAIL"
            error = {
                "code": type(exc).__name__.upper(),
                "message": str(exc),
            }
            report["errors"] = [*report["errors"], error]
            event_sink("runner.error", error)
        return self._finalize(report, temporary_dir, final_dir)

    def _ensure_time(self, deadline: float) -> None:
        if self.monotonic() >= deadline:
            raise TimeoutError("physical acceptance total timeout")

    def _finalize(
        self,
        report: dict[str, Any],
        temporary_dir: Path,
        final_dir: Path,
    ) -> dict[str, Any]:
        report["ended_at_utc"] = utc_text(self.utc_now())
        report_path = temporary_dir / "report.json"
        try:
            validate_acceptance_report(report)
        except AcceptanceReportError as exc:
            report["status"] = "FAIL"
            report["errors"] = [
                *report.get("errors", []),
                {
                    "code": "INCOMPLETE_REPORT",
                    "message": str(exc),
                },
            ]
        atomic_write_json(report_path, report)
        temporary_dir.replace(final_dir)
        return report

    def _execute_stability(
        self,
        *,
        port: int,
        work_dir: Path,
        event_sink: Callable[[str, Mapping[str, Any]], None],
        deadline: float,
    ) -> dict[str, Any]:
        report_path = work_dir / "p1-stability.json"
        command = webots_command(
            self.config.webots,
            self.config.world,
            mode="fast",
        )
        environment = {
            "MAZE_SIM_HOST": "127.0.0.1",
            "MAZE_SIM_PORT": str(port),
            "MAZE_P1_STABILITY": "1",
            "MAZE_P1_REPORT_PATH": str(report_path),
            "MAZE_P1_DURATION_S": "10",
        }
        pid_file = work_dir / "webots.pid"
        event_sink(
            "process.launch",
            {"kind": "p1", "command": command, "port": port},
        )
        with ManagedWebotsProcess(
            command=command,
            environment=environment,
            pid_file=pid_file,
            cwd=PROJECT_ROOT,
            stdout_path=work_dir / "webots.log",
        ) as process:
            remaining = max(0.1, deadline - self.monotonic())
            process.wait(timeout=min(remaining, 90.0))
            if process.returncode != 0:
                raise RuntimeError(
                    f"P1 Webots exited with {process.returncode}"
                )
        if not report_path.is_file():
            raise RuntimeError("P1 report is missing")
        metrics = json.loads(report_path.read_text(encoding="utf-8"))
        thresholds = evaluate_p1_thresholds(metrics)
        return {
            "status": (
                "PASS"
                if all(item["passed"] for item in thresholds.values())
                else "FAIL"
            ),
            "metrics": metrics,
            "thresholds": thresholds,
        }

    def _execute_scenario(
        self,
        scenario: PhysicalScenario,
        *,
        port: int,
        work_dir: Path,
        event_sink: Callable[[str, Mapping[str, Any]], None],
    ) -> dict[str, Any]:
        world_path = self.config.world.parent / scenario.world
        if not world_path.is_file():
            raise FileNotFoundError(
                f"scenario world does not exist: {world_path}"
            )
        command = webots_command(self.config.webots, world_path)
        environment = {
            "MAZE_SIM_HOST": "127.0.0.1",
            "MAZE_SIM_PORT": str(port),
            "MAZE_PHYSICAL_PROFILE_DIR": str(
                DEFAULT_PHYSICAL_PROFILE_DIR
            ),
            "MAZE_PHYSICAL_PROFILE_ID": scenario.physical_profile_id,
            "MAZE_PHYSICAL_PROFILE_DIGEST": (
                scenario.physical_profile_digest
            ),
            "MAZE_PHYSICAL_SENSOR_MODE": "ideal",
            "MAZE_ACCEPTANCE_SCREENSHOT_PATH": str(
                work_dir / "scene.png"
            ),
        }
        event_sink(
            "process.launch",
            {
                "scenario_id": scenario.scenario_id,
                "command": command,
                "port": port,
            },
        )
        with ManagedWebotsProcess(
            command=command,
            environment=environment,
            pid_file=work_dir / "webots.pid",
            cwd=PROJECT_ROOT,
            stdout_path=work_dir / "webots.log",
        ) as process:
            session = connect_device_session(
                port=port,
                process=process,
                ready_timeout_s=min(
                    30.0,
                    scenario.timeout_ms / 1000.0,
                ),
                operation_timeout_s=max(
                    5.0,
                    scenario.timeout_ms / 1000.0,
                ),
            )
            try:
                return execute_scenario_session(
                    scenario,
                    session=session,
                    event_sink=event_sink,
                )
            finally:
                session.close()


def execute_scenario_session(
    scenario: PhysicalScenario,
    *,
    session: DeviceSession,
    event_sink: Callable[[str, Mapping[str, Any]], None],
) -> dict[str, Any]:
    ready = session.wait_ready(timeout_s=5.0)
    validate_ready_identity(ready, scenario)
    event_sink("ready", ready)
    profile = PhysicalProfileRepository().get(
        scenario.physical_profile_id,
        expected_digest=scenario.physical_profile_digest,
    )
    ticks_per_mm = (
        profile.encoder.ticks_per_revolution
        / (2.0 * math.pi * profile.geometry.wheel_radius_m * 1000.0)
    )
    frames: list[dict[str, Any]] = []
    initial_frames: list[dict[str, Any]] = []
    action_records: list[dict[str, Any]] = []
    requested_actions = sum(
        action.repeat for action in scenario.actions
    )
    completed_actions = 0
    wall_started = time.monotonic()

    with session.subscribe(
        message_types={"telemetry"},
        max_queue=512,
    ) as subscription:
        reset = session.request_ack(
            "reset",
            physical_profile_id=scenario.physical_profile_id,
            physical_profile_digest=scenario.physical_profile_digest,
            map_version_id=scenario.map_version_id,
            map_digest=scenario.map_digest,
        )
        event_sink("reset.ack", reset)
        collect_frames(
            subscription,
            frames,
            event_sink=event_sink,
            count=8,
            timeout_s=2.0,
        )
        initial_frames.extend(frames[-8:])
        started = session.request_ack(
            "start",
            physical_profile_id=scenario.physical_profile_id,
            physical_profile_digest=scenario.physical_profile_digest,
            map_version_id=scenario.map_version_id,
            map_digest=scenario.map_digest,
        )
        event_sink("start.ack", started)
        heartbeat_stop = threading.Event()
        heartbeat_errors: list[str] = []
        heartbeat_thread = threading.Thread(
            target=heartbeat_loop,
            args=(session, heartbeat_stop, heartbeat_errors),
            name=f"acceptance-heartbeat-{scenario.scenario_id}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            action_number = 0
            abort_scenario = False
            for action in scenario.actions:
                for _repeat in range(action.repeat):
                    action_number += 1
                    if (
                        action_number > 1
                        and scenario_resets_between_trials(
                            scenario.scenario_id
                        )
                    ):
                        reset = session.request_ack(
                            "reset",
                            physical_profile_id=(
                                scenario.physical_profile_id
                            ),
                            physical_profile_digest=(
                                scenario.physical_profile_digest
                            ),
                            map_version_id=scenario.map_version_id,
                            map_digest=scenario.map_digest,
                        )
                        event_sink("trial.reset.ack", reset)
                        collect_frames(
                            subscription,
                            frames,
                            event_sink=event_sink,
                            count=len(frames) + 2,
                            timeout_s=1.0,
                        )
                        started = session.request_ack(
                            "start",
                            physical_profile_id=(
                                scenario.physical_profile_id
                            ),
                            physical_profile_digest=(
                                scenario.physical_profile_digest
                            ),
                            map_version_id=scenario.map_version_id,
                            map_digest=scenario.map_digest,
                        )
                        event_sink("trial.start.ack", started)
                    drain_frames(
                        subscription,
                        frames,
                        event_sink=event_sink,
                    )
                    before = latest_truth_frame(
                        frames,
                        fallback=session.last_telemetry,
                    )
                    action_id = (
                        f"{scenario.scenario_id}-{action_number:03d}"
                    )
                    event_sink(
                        "action.request",
                        {
                            "action_id": action_id,
                            "name": action.name,
                            "speed": action.speed,
                            "target_ticks": action.target_ticks,
                        },
                    )
                    try:
                        ack, result = session.execute_action_with_ack(
                            action_id=action_id,
                            name=action.name,
                            speed=action.speed,
                            target_ticks=action.target_ticks,
                        )
                    except Exception as exc:
                        action_records.append(
                            {
                                "action_id": action_id,
                                "name": action.name,
                                "success": False,
                                "error": str(exc),
                            }
                        )
                        raise
                    event_sink("action.ack", ack)
                    event_sink(str(result.get("type")), result)
                    time.sleep(0.08)
                    drain_frames(
                        subscription,
                        frames,
                        event_sink=event_sink,
                    )
                    after = latest_truth_frame(
                        frames,
                        fallback=session.last_telemetry,
                    )
                    success = (
                        result.get("type") == "done"
                        and result.get("success") is not False
                    )
                    completed_actions += int(success)
                    action_records.append(
                        evaluate_action(
                            action=action,
                            action_id=action_id,
                            success=success,
                            before=before,
                            after=after,
                            ticks_per_mm=ticks_per_mm,
                        )
                    )
                    if not success:
                        abort_scenario = True
                        break
                if abort_scenario:
                    break
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1.0)
        drain_frames(
            subscription,
            frames,
            event_sink=event_sink,
        )
    if heartbeat_errors:
        raise RuntimeError(
            f"heartbeat failed: {heartbeat_errors[-1]}"
        )
    metrics = evaluate_scenario_frames(
        frames,
        ticks_per_mm=ticks_per_mm,
        completed_actions=completed_actions,
        requested_actions=requested_actions,
    ).to_dict()
    metrics.update(action_metrics(action_records))
    metrics.update(
        runtime_metrics(
            frames,
            wall_elapsed_s=time.monotonic() - wall_started,
        )
    )
    metrics.update(patch_metrics(frames))
    metrics["configured_wheel_friction_difference"] = abs(
        profile.surface.left_wheel_friction
        - profile.surface.right_wheel_friction
    )
    metrics["configured_patch_friction_difference"] = abs(
        profile.surface.base_floor_friction
        - profile.surface.patch_friction
    )
    thresholds = evaluate_scenario_thresholds(
        scenario,
        metrics=metrics,
    )
    errors = [
        record for record in action_records if not record["success"]
    ]
    return {
        "scenario_id": scenario.scenario_id,
        "status": (
            "PASS"
            if all(item["passed"] for item in thresholds.values())
            and not errors
            else "FAIL"
        ),
        "profile_id": scenario.physical_profile_id,
        "profile_digest": scenario.physical_profile_digest,
        "map_version_id": scenario.map_version_id,
        "map_digest": scenario.map_digest,
        "seed": scenario.seed,
        "metrics": metrics,
        "thresholds": thresholds,
        "errors": errors,
        "initial_frames": initial_frames,
    }


def scenario_resets_between_trials(scenario_id: str) -> bool:
    """Keep route scenarios continuous and isolate repeatability trials."""
    return scenario_id != "local-patch-p4-v1"


def connect_device_session(
    *,
    port: int,
    process: Any,
    ready_timeout_s: float,
    operation_timeout_s: float,
) -> DeviceSession:
    deadline = time.monotonic() + ready_timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"Webots exited before ready: {process.returncode}"
            )
        try:
            stream = open_tcp(
                f"127.0.0.1:{port}",
                read_timeout_s=0.05,
                connect_timeout_s=0.25,
            )
            session = DeviceSession(
                SerialClient(
                    stream,
                    timeout_s=operation_timeout_s,
                )
            )
            session.start()
            return session
        except Exception as exc:
            last_error = exc
            time.sleep(0.1)
    raise TimeoutError(
        f"timeout waiting for Webots ready on port {port}: {last_error}"
    )


def heartbeat_loop(
    session: DeviceSession,
    stopped: threading.Event,
    errors: list[str],
) -> None:
    while not stopped.wait(0.35):
        try:
            session.send_heartbeat()
        except Exception as exc:
            errors.append(str(exc))
            return


def collect_frames(
    subscription,
    frames: list[dict[str, Any]],
    *,
    event_sink: Callable[[str, Mapping[str, Any]], None],
    count: int,
    timeout_s: float,
) -> None:
    deadline = time.monotonic() + timeout_s
    while len(frames) < count and time.monotonic() < deadline:
        frame = subscription.get(timeout_s=0.1)
        if frame is None:
            continue
        frames.append(dict(frame))
        event_sink("telemetry", frame)
    if len(frames) < count:
        raise TimeoutError(
            f"telemetry incomplete: wanted {count}, got {len(frames)}"
        )


def drain_frames(
    subscription,
    frames: list[dict[str, Any]],
    *,
    event_sink: Callable[[str, Mapping[str, Any]], None],
) -> None:
    drained = 0
    deadline = time.monotonic() + 0.05
    while drained < 256 and time.monotonic() < deadline:
        frame = subscription.get(timeout_s=0.0)
        if frame is None:
            return
        frames.append(dict(frame))
        event_sink("telemetry", frame)
        drained += 1


def latest_truth_frame(
    frames: list[dict[str, Any]],
    *,
    fallback: Mapping[str, Any] | None,
) -> dict[str, Any]:
    candidates = [*frames]
    if fallback is not None:
        candidates.append(dict(fallback))
    for frame in reversed(candidates):
        if isinstance(frame.get("sim_truth"), Mapping):
            return dict(frame)
    raise RuntimeError("telemetry has no simulation truth")


def evaluate_action(
    *,
    action,
    action_id: str,
    success: bool,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    ticks_per_mm: float,
) -> dict[str, Any]:
    before_truth = before["sim_truth"]
    after_truth = after["sim_truth"]
    x_delta = float(after_truth["x_mm"]) - float(
        before_truth["x_mm"]
    )
    y_delta = float(after_truth["y_mm"]) - float(
        before_truth["y_mm"]
    )
    yaw_delta = angle_delta(
        float(before_truth["yaw_deg"]),
        float(after_truth["yaw_deg"]),
    )
    expected_distance = action.target_ticks / ticks_per_mm
    record = {
        "action_id": action_id,
        "name": action.name,
        "success": bool(success),
        "distance_error_mm": None,
        "heading_error_deg": None,
        "turn_error_deg": None,
    }
    if action.name == "move_cell":
        record["distance_error_mm"] = abs(
            math.hypot(x_delta, y_delta) - expected_distance
        )
        record["heading_error_deg"] = abs(yaw_delta)
    else:
        expected_turn = math.degrees(
            2.0 * expected_distance / 135.0
        )
        if action.name == "turn_back":
            expected_turn = 180.0
        record["turn_error_deg"] = abs(
            abs(yaw_delta) - expected_turn
        )
    return record


def action_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "max_distance_error_mm": max_present(
            record.get("distance_error_mm") for record in records
        ),
        "max_heading_error_deg": max_present(
            record.get("heading_error_deg") for record in records
        ),
        "max_turn_error_deg": max_present(
            record.get("turn_error_deg") for record in records
        ),
    }


def runtime_metrics(
    frames: list[dict[str, Any]],
    *,
    wall_elapsed_s: float,
) -> dict[str, Any]:
    timestamps = [
        float(frame["ts_ms"])
        for frame in frames
        if frame.get("ts_ms") is not None
    ]
    periods = [
        float(frame["controller_period_ms"])
        for frame in frames
        if frame.get("controller_period_ms") is not None
    ]
    simulation_elapsed_s = (
        (max(timestamps) - min(timestamps)) / 1000.0
        if len(timestamps) >= 2
        else 0.0
    )
    return {
        "real_time_factor": (
            simulation_elapsed_s / wall_elapsed_s
            if wall_elapsed_s > 0
            else None
        ),
        "controller_period_ms": (
            sum(periods) / len(periods) if periods else None
        ),
        "telemetry_frame_count": len(frames),
    }


def patch_metrics(frames: list[dict[str, Any]]) -> dict[str, Any]:
    normal: list[float] = []
    patch: list[float] = []
    for frame in frames:
        truth = frame.get("sim_truth")
        if not isinstance(truth, Mapping):
            continue
        slip = (
            abs(float(truth.get("left_slip_rate", 0.0)))
            + abs(float(truth.get("right_slip_rate", 0.0)))
        ) / 2.0
        (
            patch
            if truth.get("active_surface") == "local_patch"
            else normal
        ).append(slip)
    return {
        "patch_slip_increase": (
            (sum(patch) / len(patch))
            - (sum(normal) / len(normal))
            if patch and normal
            else 0.0
        )
    }


def evaluate_scenario_thresholds(
    scenario: PhysicalScenario,
    *,
    metrics: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    metric_for = {
        "max_distance_error_mm": "max_distance_error_mm",
        "max_heading_error_deg": "max_heading_error_deg",
        "max_turn_error_deg": "max_turn_error_deg",
        "min_success_rate": "success_rate",
        "min_mean_abs_slip": "mean_abs_left_slip",
        "min_trajectory_difference_mm": "encoder_truth_gap_mm",
        "min_configured_friction_difference": (
            "configured_wheel_friction_difference"
        ),
        "min_configured_patch_friction_difference": (
            "configured_patch_friction_difference"
        ),
        "min_slip_difference": "mean_abs_slip_difference",
        "min_yaw_difference_deg": "final_yaw_deg",
        "min_patch_slip_increase": "patch_slip_increase",
        "min_surface_transitions": "surface_transitions",
    }
    decisions: dict[str, dict[str, Any]] = {}
    for name, limit in scenario.acceptance_thresholds:
        metric_name = metric_for[name]
        actual = metrics.get(metric_name)
        if name == "min_mean_abs_slip":
            actual = min(
                float(metrics.get("mean_abs_left_slip") or 0.0),
                float(metrics.get("mean_abs_right_slip") or 0.0),
            )
        if name == "min_yaw_difference_deg":
            actual = abs(
                angle_delta(0.0, float(actual or 0.0))
            )
        passed = (
            actual is not None
            and (
                float(actual) <= float(limit)
                if name.startswith("max_")
                else float(actual) >= float(limit)
            )
        )
        decisions[name] = {
            "metric": metric_name,
            "actual": actual,
            "limit": limit,
            "operator": "<=" if name.startswith("max_") else ">=",
            "passed": bool(passed),
        }
    return decisions


def evaluate_p1_thresholds(
    metrics: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    limits = {
        "horizontal_drift_m": 0.002,
        "orientation_delta_deg": 1.0,
        "max_vertical_speed_mps": 0.05,
        "max_tilt_deg": 5.0,
    }
    result = {
        name: {
            "actual": metrics.get(name),
            "limit": limit,
            "operator": "<=",
            "passed": (
                metrics.get(name) is not None
                and float(metrics[name]) <= limit
            ),
        }
        for name, limit in limits.items()
    }
    for name in ("fell_over", "below_floor"):
        result[name] = {
            "actual": metrics.get(name),
            "limit": False,
            "operator": "==",
            "passed": metrics.get(name) is False,
        }
    return result


def evaluate_p2(
    frames: list[dict[str, Any]],
) -> dict[str, Any]:
    expected = {"front_mm": 536.0, "left_mm": 574.0, "right_mm": 574.0}
    values = {
        name: [
            float(frame[name])
            for frame in frames
            if frame.get(name) is not None
        ]
        for name in expected
    }
    errors = {
        name: (
            max(abs(value - expected[name]) for value in samples)
            if samples
            else None
        )
        for name, samples in values.items()
    }
    spreads = {
        name: (
            max(samples) - min(samples) if samples else None
        )
        for name, samples in values.items()
    }
    max_error = max_present(errors.values())
    max_spread = max_present(spreads.values())
    thresholds = {
        "max_tof_error_mm": {
            "actual": max_error,
            "limit": 10.0,
            "operator": "<=",
            "passed": max_error is not None and max_error <= 10.0,
        },
        "fixed_seed_repeatability_mm": {
            "actual": max_spread,
            "limit": 0.0,
            "operator": "<=",
            "passed": max_spread is not None and max_spread <= 0.0,
        },
        "minimum_frames": {
            "actual": len(frames),
            "limit": 2,
            "operator": ">=",
            "passed": len(frames) >= 2,
        },
    }
    return {
        "status": (
            "PASS"
            if all(item["passed"] for item in thresholds.values())
            else "FAIL"
        ),
        "metrics": {
            "frame_count": len(frames),
            "expected_mm": expected,
            "max_error_by_sensor_mm": errors,
            "max_repeatability_spread_by_sensor_mm": spreads,
            "max_tof_error_mm": max_error,
            "max_repeatability_spread_mm": max_spread,
        },
        "thresholds": thresholds,
    }


def aggregate_performance(
    scenarios: list[Mapping[str, Any]],
) -> dict[str, Any]:
    metrics = [
        item.get("metrics")
        for item in scenarios
        if isinstance(item.get("metrics"), Mapping)
    ]
    real_time = [
        float(item["real_time_factor"])
        for item in metrics
        if item.get("real_time_factor") is not None
    ]
    periods = [
        float(item["controller_period_ms"])
        for item in metrics
        if item.get("controller_period_ms") is not None
    ]
    return {
        "real_time_factor": (
            sum(real_time) / len(real_time) if real_time else None
        ),
        "controller_period_ms": (
            sum(periods) / len(periods) if periods else None
        ),
    }


def unique_profiles(
    scenarios: tuple[PhysicalScenario, ...],
    *,
    profiles: PhysicalProfileRepository,
) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for scenario in scenarios:
        profile = profiles.get(
            scenario.physical_profile_id,
            expected_digest=scenario.physical_profile_digest,
        )
        records[profile.profile_id] = {
            "profile_id": profile.profile_id,
            "digest": profile.digest,
            "seed": profile.random_seed,
        }
    return [records[key] for key in sorted(records)]


def unique_maps(
    scenarios: tuple[PhysicalScenario, ...],
) -> list[dict[str, Any]]:
    records = {
        scenario.map_version_id: {
            "map_version_id": scenario.map_version_id,
            "digest": scenario.map_digest,
        }
        for scenario in scenarios
    }
    return [records[key] for key in sorted(records)]


def validate_ready_identity(
    ready: Mapping[str, Any],
    scenario: PhysicalScenario,
) -> None:
    expected = {
        "simulation_backend": "physical",
        "physical_profile_id": scenario.physical_profile_id,
        "physical_profile_digest": scenario.physical_profile_digest,
        "map_version_id": scenario.map_version_id,
        "map_digest": scenario.map_digest,
    }
    mismatches = {
        name: {"expected": value, "actual": ready.get(name)}
        for name, value in expected.items()
        if ready.get(name) != value
    }
    if mismatches:
        raise RuntimeError(
            f"Webots ready identity mismatch: {mismatches}"
        )


def webots_command(
    webots: Path,
    world: Path,
    *,
    mode: str = "realtime",
) -> list[str]:
    if mode not in {"fast", "realtime"}:
        raise ValueError(f"unsupported Webots mode: {mode}")
    command = [
        str(webots),
        "--batch",
        "--stdout",
        "--stderr",
        f"--mode={mode}",
        "--no-rendering",
        str(world.resolve()),
    ]
    xvfb = shutil.which("xvfb-run")
    if not os.environ.get("DISPLAY") and xvfb:
        return [
            xvfb,
            "-a",
            "-s",
            "-screen 0 1280x800x24",
            *command,
        ]
    return command


def reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def detect_webots_version(webots: Path) -> str:
    command = [str(webots), "--version"]
    xvfb = shutil.which("xvfb-run")
    if not os.environ.get("DISPLAY") and xvfb:
        command = [xvfb, "-a", *command]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    output = (result.stdout or result.stderr).strip()
    if result.returncode != 0 or not output:
        raise RuntimeError("cannot determine Webots version")
    return output.splitlines()[0]


def detect_source_commit() -> str:
    result = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return (
        result.stdout.strip()
        if result.returncode == 0 and result.stdout.strip()
        else "unknown"
    )


def executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def max_present(values) -> float | None:
    numbers = [
        float(value) for value in values if value is not None
    ]
    return max(numbers) if numbers else None


def angle_delta(start_deg: float, end_deg: float) -> float:
    return (float(end_deg) - float(start_deg) + 180.0) % 360.0 - 180.0


def unavailable_stage() -> dict[str, Any]:
    return {
        "status": "unavailable",
        "metrics": {},
        "thresholds": {},
    }


def utc_text(value: datetime) -> str:
    return (
        value.astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        stream.flush()


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def safe_call(callback: Callable[[], str], *, fallback: str) -> str:
    with suppress(Exception):
        value = str(callback()).strip()
        if value:
            return value
    return fallback


def exit_code_for_report(report: Mapping[str, Any]) -> int:
    return 0 if report.get("status") == "PASS" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run isolated Webots physical acceptance",
    )
    parser.add_argument("--webots", type=Path, required=True)
    parser.add_argument("--world", type=Path, default=DEFAULT_WORLD)
    parser.add_argument(
        "--scenarios",
        type=Path,
        default=DEFAULT_SCENARIOS,
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=600.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = AcceptanceRunConfig(
        webots=args.webots,
        world=args.world,
        scenarios=args.scenarios,
        output=args.output,
        total_timeout_s=args.timeout,
    )
    report = PhysicalAcceptanceRunner(config).run()
    print(
        json.dumps(
            {
                "run_id": report["run_id"],
                "status": report["status"],
                "output": str(config.output / report["run_id"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return exit_code_for_report(report)


if __name__ == "__main__":
    sys.exit(main())
