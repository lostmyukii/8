"""Run two isolated, authenticated HTTP-to-Webots map-goal P5 trials."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import secrets
import socket
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from rdk_maze_tuner.core.maze_validation import validate_map_definition
from rdk_maze_tuner.core.param_manager import ParamManager
from rdk_maze_tuner.platform.auth import AuthService
from rdk_maze_tuner.platform.database import Database
from rdk_maze_tuner.platform.map_repository import MapRepository
from simulation.webots.maze_car.physical_config import (
    PhysicalProfileRepository,
)

from .goal_acceptance_schema import (
    GoalAcceptanceReportError,
    validate_goal_acceptance_report,
)
from .run_physical_acceptance import (
    ManagedWebotsProcess,
    append_jsonl,
    atomic_write_json,
    detect_source_commit,
    detect_webots_version,
    executable_file,
    reserve_loopback_port,
    utc_text,
    webots_command,
)


PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_WORLD = (
    PROJECT_ROOT
    / "simulation/webots/maze_car/worlds/"
    "maze_physical_calibration.wbt"
)
DEFAULT_MAP = (
    PROJECT_ROOT
    / "simulation/webots/maze_car/config/maps/"
    "task12-public-v2.json"
)
DEFAULT_CONFIG = (
    PROJECT_ROOT
    / "simulation/webots/maze_car/config/"
    "goal_acceptance.yaml"
)
DEFAULT_PARAMS = PROJECT_ROOT / "rdk_maze_tuner/config/params.yaml"
DEFAULT_LIMITS = PROJECT_ROOT / "rdk_maze_tuner/config/limits.yaml"


@dataclass(frozen=True)
class GoalAcceptanceRunConfig:
    webots: Path
    world: Path
    map_asset: Path
    acceptance_config: Path
    output: Path
    total_timeout_s: float = 600.0


class GoalAcceptanceRunner:
    def __init__(
        self,
        config: GoalAcceptanceRunConfig,
        *,
        trial_executor: Callable[..., dict[str, Any]] | None = None,
        source_commit: Callable[[], str] = detect_source_commit,
        webots_version: Callable[[Path], str] = detect_webots_version,
        profile_loader: Callable[[str], Mapping[str, Any]]
        | None = None,
        param_snapshot: Callable[[], Mapping[str, Any]]
        | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.trial_executor = (
            trial_executor or self._execute_http_trial
        )
        self.source_commit = source_commit
        self.webots_version = webots_version
        self.profile_loader = profile_loader or load_profile_evidence
        self.param_snapshot = param_snapshot or load_param_evidence
        self.monotonic = monotonic
        self.utc_now = utc_now or (lambda: datetime.now(UTC))

    def run(self) -> dict[str, Any]:
        started = self.utc_now()
        run_id = (
            "goal-"
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

        def event_sink(
            event_type: str,
            payload: Mapping[str, Any],
        ) -> None:
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
            "source_commit": safe_text(
                self.source_commit,
                fallback="unknown",
            ),
            "webots_version": "unavailable",
            "started_at_utc": utc_text(started),
            "ended_at_utc": utc_text(started),
            "output_dir": run_id,
            "map": {
                "map_version_id": "unavailable",
                "digest": "0" * 64,
            },
            "param_version": {
                "version_id": "unavailable",
                "digest": "0" * 64,
            },
            "completion_thresholds": {},
            "physical_profile": {
                "profile_id": "unavailable",
                "digest": "0" * 64,
                "seed": 0,
            },
            "start": {"cell": [0, 4], "heading": "N"},
            "goal": {
                "cell": [4, 0],
                "source": "map_primary_goal",
            },
            "truth_policy": {
                "sim_truth": "evaluation_only",
                "algorithm_evidence_excludes_sim_truth": True,
            },
            "trials": [],
            "errors": [],
            "artifacts": {
                "report_json": "report.json",
                "events_jsonl": "events.jsonl",
            },
        }
        report = dict(base)
        try:
            settings = load_acceptance_config(
                self.config.acceptance_config
            )
            definition = load_map_asset(self.config.map_asset)
            digest = definition.content_digest
            if digest != settings["map_digest"]:
                raise ValueError(
                    "map asset digest does not match controlled P5 config"
                )
            if (
                [definition.start.x, definition.start.y] != [0, 4]
                or definition.start.heading != "N"
                or [4, 0] not in [
                    list(goal) for goal in definition.goals
                ]
            ):
                raise ValueError(
                    "P5 map must start at (0,4) N and own goal (4,0)"
                )
            report["map"] = {
                "map_version_id": settings["map_version_id"],
                "digest": digest,
            }
            params = dict(self.param_snapshot())
            if str(settings["param_version_id"]) != str(
                params["version_id"]
            ):
                raise ValueError(
                    "configured parameter version does not match snapshot"
                )
            report["param_version"] = {
                "version_id": str(params["version_id"]),
                "digest": str(params["digest"]),
            }
            report["completion_thresholds"] = dict(
                params["completion_thresholds"]
            )
            profile = dict(
                self.profile_loader(settings["physical_profile_id"])
            )
            report["physical_profile"] = {
                "profile_id": str(profile["profile_id"]),
                "digest": str(profile["digest"]),
                "seed": int(profile["seed"]),
            }
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
            trials: list[dict[str, Any]] = []
            for trial_index in range(
                1,
                int(settings["trials"]) + 1,
            ):
                if self.monotonic() >= deadline:
                    raise TimeoutError("P5 total timeout")
                work_dir = temporary_dir / f"trials/{trial_index}"
                work_dir.mkdir(parents=True, exist_ok=False)
                event_sink(
                    "trial.start",
                    {"trial_index": trial_index},
                )
                result = self.trial_executor(
                    trial_index=trial_index,
                    work_dir=work_dir,
                    map_definition=definition.to_dict(),
                    map_version_id=settings["map_version_id"],
                    map_digest=digest,
                    param_version_id=str(params["version_id"]),
                    physical_profile_id=str(profile["profile_id"]),
                    profile_digest=str(profile["digest"]),
                    seed=int(profile["seed"]),
                    max_steps=int(settings["max_steps"]),
                    task_timeout_s=min(
                        float(settings["task_timeout_s"]),
                        max(1.0, deadline - self.monotonic()),
                    ),
                    event_sink=event_sink,
                )
                trials.append(dict(result))
                event_sink(
                    "trial.complete",
                    {
                        "trial_index": trial_index,
                        "status": result.get("status"),
                    },
                )
            report["trials"] = trials
            report["status"] = (
                "PASS"
                if len(trials) == 2
                and all(
                    trial.get("status") == "PASS"
                    for trial in trials
                )
                else "FAIL"
            )
        except Exception as exc:
            error = {
                "code": type(exc).__name__.upper(),
                "message": str(exc),
            }
            report["status"] = "FAIL"
            report["errors"] = [*report["errors"], error]
            event_sink("runner.error", error)
        return self._finalize(report, temporary_dir, final_dir)

    def _finalize(
        self,
        report: dict[str, Any],
        temporary_dir: Path,
        final_dir: Path,
    ) -> dict[str, Any]:
        report["ended_at_utc"] = utc_text(self.utc_now())
        try:
            validate_goal_acceptance_report(report)
        except GoalAcceptanceReportError as exc:
            if report.get("status") != "unavailable":
                report["status"] = "FAIL"
                report["errors"] = [
                    *report.get("errors", []),
                    {
                        "code": "INCOMPLETE_REPORT",
                        "message": str(exc),
                    },
                ]
        atomic_write_json(temporary_dir / "report.json", report)
        temporary_dir.replace(final_dir)
        return report

    def _execute_http_trial(
        self,
        *,
        trial_index: int,
        work_dir: Path,
        map_definition: Mapping[str, Any],
        map_version_id: str,
        map_digest: str,
        param_version_id: str,
        physical_profile_id: str,
        profile_digest: str,
        seed: int,
        max_steps: int,
        task_timeout_s: float,
        event_sink: Callable[[str, Mapping[str, Any]], None],
    ) -> dict[str, Any]:
        sim_port = reserve_loopback_port()
        dashboard_port = reserve_loopback_port()
        data_dir = work_dir / "data"
        database = Database(data_dir / "maze-platform.sqlite3")
        database.initialize()
        username = f"p5-{trial_index}-{secrets.token_hex(4)}"
        password = secrets.token_urlsafe(24) + "Aa1!"
        user = AuthService(database=database).create_user(
            username,
            password,
        )
        repository = MapRepository(
            database=database,
            artifacts_dir=data_dir / "artifacts",
        )
        _, stored = repository.create_map(
            name=f"P5 isolated {map_version_id}",
            definition=map_definition,
            created_by_user_id=user.user_id,
        )
        if stored.digest != map_digest:
            raise RuntimeError("isolated map import changed digest")

        webots = ManagedWebotsProcess(
            command=goal_webots_command(
                self.config.webots,
                self.config.world,
            ),
            environment={
                "MAZE_SIM_HOST": "127.0.0.1",
                "MAZE_SIM_PORT": str(sim_port),
                "MAZE_PHYSICAL_PROFILE_ID": physical_profile_id,
                "MAZE_PHYSICAL_PROFILE_DIGEST": profile_digest,
                "MAZE_PHYSICAL_SENSOR_MODE": "ideal",
            },
            pid_file=work_dir / "webots.pid",
            cwd=PROJECT_ROOT,
            stdout_path=work_dir / "webots.log",
        )
        with webots as webots_process:
            wait_for_port(
                "127.0.0.1",
                sim_port,
                process=webots_process,
                timeout_s=30.0,
            )
            dashboard = ManagedWebotsProcess(
                command=[
                    sys.executable,
                    "-m",
                    "rdk_maze_tuner.dashboard.app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(dashboard_port),
                    "--tcp",
                    f"127.0.0.1:{sim_port}",
                    "--timeout",
                    "12",
                ],
                environment={
                    "MAZE_DATA_DIR": str(data_dir),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                pid_file=work_dir / "dashboard.pid",
                cwd=PROJECT_ROOT,
                stdout_path=work_dir / "dashboard.log",
            )
            with dashboard as dashboard_process:
                wait_for_http(
                    dashboard_port,
                    process=dashboard_process,
                    timeout_s=30.0,
                )
                client = P5HttpClient(port=dashboard_port)
                client.login(username=username, password=password)
                lease = client.post("/api/control/claim")
                client.lease_token = str(lease["lease_token"])
                created = client.post(
                    "/api/tasks",
                    {
                        "run_kind": "auto_to_map_goal",
                        "mode": "simulation",
                        "map_version": stored.version_id,
                        "param_version": param_version_id,
                        "max_steps": max_steps,
                        "physical_profile_id": physical_profile_id,
                    },
                )
                task_id = str(created["task_id"])
                client.post(f"/api/tasks/{task_id}/preflight")
                client.post("/api/control/heartbeat")
                reset = client.post(f"/api/tasks/{task_id}/reset")
                run_id = str(reset["run_id"])
                client.post("/api/control/heartbeat")
                client.post(f"/api/tasks/{task_id}/start")
                terminal = poll_task(
                    client,
                    task_id=task_id,
                    timeout_s=task_timeout_s,
                )
                run = client.get(f"/api/runs/{run_id}")
                events_payload = client.get(
                    f"/api/runs/{run_id}/events"
                )
                replay = client.get(f"/api/runs/{run_id}/replay")

        events = list(events_payload["events"])
        atomic_write_json(work_dir / "replay.json", replay)
        write_events_jsonl(work_dir / "events.jsonl", events)
        return build_trial_evidence(
            trial_index=trial_index,
            task_id=task_id,
            run_id=run_id,
            terminal=terminal,
            run=run,
            events=events,
            replay=replay,
            map_definition=map_definition,
            raw_events_path=(
                f"trials/{trial_index}/events.jsonl"
            ),
            replay_path=f"trials/{trial_index}/replay.json",
        )


class P5HttpClient:
    """Tiny JSON client that explicitly carries the Secure session cookie."""

    def __init__(self, *, port: int) -> None:
        self.port = int(port)
        self.session_cookie: str | None = None
        self.csrf_token: str | None = None
        self.lease_token: str | None = None

    def login(self, *, username: str, password: str) -> dict[str, Any]:
        payload, headers = self._request(
            "POST",
            "/api/auth/login",
            {"username": username, "password": password},
            authenticated=False,
        )
        cookie = headers.get("set-cookie", "").split(";", 1)[0]
        if not cookie.startswith("maze_session="):
            raise RuntimeError("login did not return a session cookie")
        self.session_cookie = cookie
        self.csrf_token = str(payload["csrf_token"])
        return payload

    def get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path, None)[0]

    def post(
        self,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request("POST", path, payload or {})[0]

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None,
        *,
        authenticated: bool = True,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        body = (
            None
            if payload is None
            else json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if authenticated:
            if self.session_cookie is None:
                raise RuntimeError("HTTP client is not authenticated")
            headers["Cookie"] = self.session_cookie
            if method != "GET":
                headers["X-CSRF-Token"] = str(self.csrf_token or "")
                if self.lease_token:
                    headers["X-Control-Lease"] = self.lease_token
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.port,
            timeout=20.0,
        )
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            response_headers = {
                name.lower(): value
                for name, value in response.getheaders()
            }
        finally:
            connection.close()
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"{method} {path} returned invalid JSON"
            ) from exc
        if not 200 <= response.status < 300:
            raise RuntimeError(
                f"{method} {path} failed with {response.status}: {result}"
            )
        if not isinstance(result, dict):
            raise RuntimeError(f"{method} {path} returned non-object JSON")
        return result, response_headers


def build_trial_evidence(
    *,
    trial_index: int,
    task_id: str,
    run_id: str,
    terminal: Mapping[str, Any],
    run: Mapping[str, Any],
    events: list[Mapping[str, Any]],
    replay: Mapping[str, Any],
    map_definition: Mapping[str, Any],
    raw_events_path: str,
    replay_path: str,
) -> dict[str, Any]:
    route = next(
        (
            event["payload"]["cells"]
            for event in events
            if event.get("type") == "route.planned"
            and isinstance(event.get("payload"), Mapping)
            and isinstance(event["payload"].get("cells"), list)
        ),
        [],
    )
    terminals: dict[str, Mapping[str, Any]] = {}
    for event in events:
        if event.get("type") in {"done", "error"}:
            payload = event.get("payload")
            if isinstance(payload, Mapping) and payload.get("action_id"):
                terminals[str(payload["action_id"])] = payload
        elif event.get("type") == "motion.recovery.done":
            payload = event.get("payload")
            result = (
                payload.get("result")
                if isinstance(payload, Mapping)
                else None
            )
            if isinstance(result, Mapping) and result.get("action_id"):
                terminals[str(result["action_id"])] = result
    actions = []
    for event in events:
        event_type = event.get("type")
        if event_type not in {
            "planned_action",
            "motion.recovery.started",
        }:
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        recovery = event_type == "motion.recovery.started"
        action_id = str(
            payload.get(
                "recovery_action_id" if recovery else "action_id"
            )
            or ""
        )
        if not action_id:
            continue
        actions.append(
            {
                "action_id": action_id,
                "name": str(
                    payload.get("action_name" if recovery else "name")
                    or ("move_distance" if recovery else "")
                ),
                "recovery": recovery,
                "terminal": dict(terminals.get(action_id, {})),
            }
        )
    corrections = recovery_evidence(events)
    final_pose = next(
        (
            dict(event["payload"])
            for event in reversed(events)
            if event.get("type") == "pose.committed"
            and isinstance(event.get("payload"), Mapping)
        ),
        {},
    )
    telemetry = [
        event["payload"]
        for event in events
        if event.get("type") == "telemetry"
        and isinstance(event.get("payload"), Mapping)
    ]
    safety_evaluation = evaluate_truth_safety(
        telemetry,
        map_definition=map_definition,
    )
    conflict_count = sum(
        event.get("type") == "map_sensor_conflict"
        for event in events
    )
    final = {
        "reliable_cell": final_pose.get("grid_cell"),
        "x_mm": final_pose.get("x_mm"),
        "y_mm": final_pose.get("y_mm"),
        "heading": final_pose.get("heading"),
        "yaw_deg": final_pose.get("yaw_deg"),
        "confidence": final_pose.get("confidence"),
    }
    completion_reason = next(
        (
            str(event.get("payload", {}).get("reason"))
            for event in reversed(events)
            if event.get("type") == "task.completed"
            and isinstance(event.get("payload"), Mapping)
        ),
        str(terminal.get("reason") or ""),
    )
    result = {
        "trial_index": int(trial_index),
        "status": "PASS",
        "task_id": str(task_id),
        "run_id": str(run_id),
        "task_status": str(terminal.get("status") or ""),
        "completion_reason": completion_reason,
        "route": route,
        "action_count": len(actions),
        "actions": actions,
        "turn_count": sum(
            action["name"] in {"turn_left", "turn_right", "turn_back"}
            for action in actions
        ),
        "corrections": corrections,
        "final_pose": final,
        "safety": {
            **safety_evaluation,
            "conflict_count": conflict_count,
        },
        "evidence_sources": [
            "encoder",
            "tof_front",
            "tof_left",
            "tof_right",
            "wall_constraint",
        ],
        "score": dict(
            run.get("latest_score")
            if isinstance(run.get("latest_score"), Mapping)
            else {}
        ),
        "replay": {
            "schema_version": int(replay.get("schema_version") or 0),
            "relative_path": replay_path,
        },
        "raw_events_jsonl": raw_events_path,
    }
    try:
        validate_goal_acceptance_report(
            {
                **minimal_report_for_trial(result),
                "trials": [result, result],
            }
        )
    except GoalAcceptanceReportError:
        result["status"] = "FAIL"
    return result


def recovery_evidence(
    events: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    results = []
    for index, event in enumerate(events):
        if event.get("type") != "motion.recovery.started":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        before = nearest_motion_evidence(events, index, reverse=True)
        after = nearest_motion_evidence(events, index, reverse=False)
        if before is None or after is None:
            continue
        results.append(
            {
                "action_id": str(payload.get("recovery_action_id") or ""),
                "kind": str(
                    payload.get("source")
                    or payload.get("action_name")
                    or "bounded_recovery"
                ),
                "before_error": error_metrics(before),
                "after_error": error_metrics(after),
            }
        )
    return results


def nearest_motion_evidence(
    events: list[Mapping[str, Any]],
    index: int,
    *,
    reverse: bool,
) -> Mapping[str, Any] | None:
    positions = (
        range(index - 1, -1, -1)
        if reverse
        else range(index + 1, len(events))
    )
    for position in positions:
        event = events[position]
        if (
            event.get("type") == "motion_evidence"
            and isinstance(event.get("payload"), Mapping)
        ):
            return event["payload"]
    return None


def error_metrics(value: Mapping[str, Any]) -> dict[str, float]:
    return {
        "position_error_ratio": float(
            value.get("position_error_ratio") or 0.0
        ),
        "heading_error_deg": float(
            value.get("heading_error_deg") or 0.0
        ),
    }


def minimal_report_for_trial(trial: Mapping[str, Any]) -> dict[str, Any]:
    """Only used to fail-close an executor result before aggregation."""
    thresholds = load_param_evidence()["completion_thresholds"]
    profile = load_profile_evidence("normal-v1")
    return {
        "schema_version": 1,
        "run_id": "trial-check",
        "status": "PASS",
        "source_commit": "unknown",
        "webots_version": "trial-check",
        "started_at_utc": "trial-check",
        "ended_at_utc": "trial-check",
        "output_dir": "trial-check",
        "map": {
            "map_version_id": "task12-public-v2",
            "digest": load_map_asset(DEFAULT_MAP).content_digest,
        },
        "param_version": {
            "version_id": "1",
            "digest": load_param_evidence()["digest"],
        },
        "completion_thresholds": thresholds,
        "physical_profile": profile,
        "start": {"cell": [0, 4], "heading": "N"},
        "goal": {"cell": [4, 0], "source": "map_primary_goal"},
        "truth_policy": {
            "sim_truth": "evaluation_only",
            "algorithm_evidence_excludes_sim_truth": True,
        },
        "trials": [dict(trial), dict(trial)],
        "errors": [],
        "artifacts": {
            "report_json": "report.json",
            "events_jsonl": "events.jsonl",
        },
    }


def load_acceptance_config(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
        raise ValueError("goal acceptance config schema_version must be 1")
    required = {
        "map_version_id",
        "map_digest",
        "param_version_id",
        "physical_profile_id",
        "trials",
    }
    missing = required - set(raw)
    if missing:
        raise ValueError(
            f"goal acceptance config missing: {sorted(missing)}"
        )
    result = {
        **dict(raw),
        "max_steps": int(raw.get("max_steps", 500)),
        "task_timeout_s": float(raw.get("task_timeout_s", 240)),
    }
    if int(result["trials"]) != 2:
        raise ValueError("P5 requires exactly two fixed-input trials")
    return result


def goal_webots_command(webots: Path, world: Path) -> list[str]:
    """Keep Webots time aligned with the Dashboard heartbeat clock."""

    return webots_command(webots, world, mode="realtime")


def load_map_asset(path: Path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_map_definition(raw)


def load_profile_evidence(profile_id: str) -> dict[str, Any]:
    profile = PhysicalProfileRepository().get(profile_id)
    return {
        "profile_id": profile.profile_id,
        "digest": profile.digest,
        "seed": profile.random_seed,
    }


def load_param_evidence() -> dict[str, Any]:
    params = ParamManager(
        params_path=DEFAULT_PARAMS,
        limits_path=DEFAULT_LIMITS,
    )
    snapshot = params.snapshot()["params"]
    canonical = json.dumps(
        snapshot,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {
        "version_id": str(params.param_version),
        "digest": hashlib.sha256(canonical).hexdigest(),
        "completion_thresholds": (
            params.arrival_verification_config().to_dict()
        ),
    }


def poll_task(
    client: P5HttpClient,
    *,
    task_id: str,
    timeout_s: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + float(timeout_s)
    next_heartbeat = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        state = client.get("/api/state")
        task = next(
            (
                item
                for item in state.get("tasks", [])
                if item.get("task_id") == task_id
            ),
            None,
        )
        if isinstance(task, Mapping) and task.get("status") in {
            "COMPLETED",
            "ERROR",
            "LOST",
            "ESTOP",
        }:
            return dict(task)
        if time.monotonic() >= next_heartbeat:
            client.post("/api/control/heartbeat")
            next_heartbeat = time.monotonic() + 3.0
        time.sleep(0.2)
    raise TimeoutError(f"task did not finish: {task_id}")


def wait_for_port(
    host: str,
    port: int,
    *,
    process,
    timeout_s: float,
) -> None:
    deadline = time.monotonic() + float(timeout_s)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"process exited before TCP ready: {process.returncode}"
            )
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"TCP endpoint did not become ready: {host}:{port}")


def wait_for_http(
    port: int,
    *,
    process,
    timeout_s: float,
) -> None:
    deadline = time.monotonic() + float(timeout_s)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"dashboard exited before HTTP ready: {process.returncode}"
            )
        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1",
                port,
                timeout=1.0,
            )
            connection.request("GET", "/api/health")
            response = connection.getresponse()
            response.read()
            connection.close()
            if response.status == 200:
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise TimeoutError("candidate Dashboard did not become ready")


def count_events(
    events: list[Mapping[str, Any]],
    event_type: str,
) -> int:
    return sum(event.get("type") == event_type for event in events)


def evaluate_truth_safety(
    telemetry: list[Mapping[str, Any]],
    *,
    map_definition: Mapping[str, Any],
) -> dict[str, int]:
    """Evaluate physics safety without feeding truth into control."""

    definition = validate_map_definition(map_definition)
    truths = [
        frame["sim_truth"]
        for frame in telemetry
        if isinstance(frame.get("sim_truth"), Mapping)
    ]
    collision_count = max(
        (int(item.get("collision_count") or 0) for item in truths),
        default=0,
    )
    out_of_bounds_count = 0
    wall_crossing_count = 0
    previous_cell: tuple[int, int] | None = None
    half_width = definition.cols * definition.cell_width_mm / 2.0
    half_height = definition.rows * definition.cell_height_mm / 2.0
    for truth in truths:
        x_mm = float(truth["x_mm"])
        y_mm = float(truth["y_mm"])
        if (
            x_mm < -half_width
            or x_mm > half_width
            or y_mm < -half_height
            or y_mm > half_height
        ):
            out_of_bounds_count += 1
            previous_cell = None
            continue
        cell = (
            int(
                round(
                    x_mm / definition.cell_width_mm
                    + (definition.cols - 1) / 2.0
                )
            ),
            int(
                round(
                    y_mm / definition.cell_height_mm
                    + (definition.rows - 1) / 2.0
                )
            ),
        )
        if not (
            0 <= cell[0] < definition.cols
            and 0 <= cell[1] < definition.rows
        ):
            out_of_bounds_count += 1
            previous_cell = None
            continue
        if previous_cell is not None and cell != previous_cell:
            delta = (
                cell[0] - previous_cell[0],
                cell[1] - previous_cell[1],
            )
            direction = {
                (0, -1): "N",
                (1, 0): "E",
                (0, 1): "S",
                (-1, 0): "W",
            }.get(delta)
            if (
                direction is None
                or direction
                in definition.blocked_directions(previous_cell)
            ):
                wall_crossing_count += 1
        previous_cell = cell
    return {
        "truth_sample_count": len(truths),
        "collision_count": collision_count,
        "out_of_bounds_count": out_of_bounds_count,
        "wall_crossing_count": wall_crossing_count,
    }


def write_events_jsonl(
    path: Path,
    events: list[Mapping[str, Any]],
) -> None:
    for event in events:
        append_jsonl(path, event)


def safe_text(
    callback: Callable[[], str],
    *,
    fallback: str,
) -> str:
    try:
        value = str(callback()).strip()
    except Exception:
        return fallback
    return value or fallback


def exit_code_for_report(report: Mapping[str, Any]) -> int:
    return 0 if report.get("status") == "PASS" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run isolated P5 map-goal acceptance",
    )
    parser.add_argument("--webots", type=Path, required=True)
    parser.add_argument("--world", type=Path, default=DEFAULT_WORLD)
    parser.add_argument("--map", dest="map_asset", type=Path, default=DEFAULT_MAP)
    parser.add_argument(
        "--config",
        dest="acceptance_config",
        type=Path,
        default=DEFAULT_CONFIG,
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=600.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = GoalAcceptanceRunner(
        GoalAcceptanceRunConfig(
            webots=args.webots,
            world=args.world,
            map_asset=args.map_asset,
            acceptance_config=args.acceptance_config,
            output=args.output,
            total_timeout_s=args.timeout,
        )
    ).run()
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
        )
    )
    return exit_code_for_report(report)


if __name__ == "__main__":
    raise SystemExit(main())
