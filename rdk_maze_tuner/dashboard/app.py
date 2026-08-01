"""FastAPI application for the maze tuning dashboard."""

from __future__ import annotations

import argparse
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from rdk_maze_tuner.core.device_session import DeviceSession
from rdk_maze_tuner.core.maze_map import MazeMap
from rdk_maze_tuner.core.maze_planner import MazePlanner
from rdk_maze_tuner.core.maze_runner import MazeRunner
from rdk_maze_tuner.core.param_manager import ParamManager, ParamValidationError
from rdk_maze_tuner.core.serial_client import SerialClient, SerialClientError, open_serial
from rdk_maze_tuner.core.tcp_stream import open_tcp
from rdk_maze_tuner.dashboard.runtime import SerialDashboardRuntime
from rdk_maze_tuner.dashboard.routes.auth import (
    AuthContext,
    create_auth_router,
)
from rdk_maze_tuner.dashboard.routes.control import (
    CONTROL_LEASE_HEADER_NAME,
    create_control_router,
)
from rdk_maze_tuner.dashboard.routes.maps import create_maps_router
from rdk_maze_tuner.dashboard.routes.physical_profiles import (
    create_physical_profiles_router,
)
from rdk_maze_tuner.dashboard.routes.runs import create_runs_router
from rdk_maze_tuner.dashboard.routes.tasks import create_tasks_router
from rdk_maze_tuner.dashboard.state import DashboardState
from rdk_maze_tuner.platform.auth import (
    AuthService,
    LoginRateLimiter,
    SessionPrincipal,
)
from rdk_maze_tuner.platform.config import PlatformConfig
from rdk_maze_tuner.platform.control_lease import (
    ControlLeaseService,
    LeasePermissionError,
)
from rdk_maze_tuner.platform.database import Database
from rdk_maze_tuner.platform.event_store import EventStore
from rdk_maze_tuner.platform.modes import (
    ModeAdapterError,
    RealModeAdapter,
    SimulationModeAdapter,
)
from rdk_maze_tuner.platform.map_repository import MapRepository
from rdk_maze_tuner.platform.physical_profile_repository import (
    PhysicalProfileRepository,
)
from rdk_maze_tuner.platform.replay import (
    ReplayService,
    RunFinalizer,
)
from rdk_maze_tuner.platform.retention import RetentionManager
from rdk_maze_tuner.platform.scoring import ScoringService
from rdk_maze_tuner.platform.task_orchestrator import (
    TaskConflictError,
    TaskOrchestrator,
    TaskRecord,
)


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
DEFAULT_PARAMS = PROJECT_DIR / "config" / "params.yaml"
DEFAULT_LIMITS = PROJECT_DIR / "config" / "limits.yaml"
TEMPLATE_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"


def create_app(
    *,
    params_path: Path = DEFAULT_PARAMS,
    limits_path: Path = DEFAULT_LIMITS,
    client: Optional[SerialClient | DeviceSession] = None,
    state: Optional[DashboardState] = None,
    database: Optional[Database] = None,
    auth_service: Optional[AuthService] = None,
    control_lease_service: Optional[ControlLeaseService] = None,
    login_rate_limiter: Optional[LoginRateLimiter] = None,
    task_orchestrator: Optional[TaskOrchestrator] = None,
    map_repository: Optional[MapRepository] = None,
    physical_profile_repository: Optional[
        PhysicalProfileRepository
    ] = None,
    scoring_service: Optional[ScoringService] = None,
    replay_service: Optional[ReplayService] = None,
    retention_manager: Optional[RetentionManager] = None,
    client_mode: Optional[str] = None,
) -> FastAPI:
    resolved_database = (
        database
        or (auth_service.database if auth_service is not None else None)
        or (
            control_lease_service.database
            if control_lease_service is not None
            else None
        )
        or Database(PlatformConfig.from_env().database_path)
    )
    resolved_database.initialize()
    resolved_auth = auth_service or AuthService(database=resolved_database)
    resolved_leases = control_lease_service or ControlLeaseService(
        database=resolved_database
    )
    auth_context = AuthContext(
        service=resolved_auth,
        rate_limiter=login_rate_limiter or LoginRateLimiter(),
    )
    params = ParamManager(params_path=params_path, limits_path=limits_path)
    coordinated_client = (
        client
        if isinstance(client, DeviceSession)
        else DeviceSession(client)
        if client is not None
        else None
    )
    dashboard_state = state or DashboardState(
        params=params,
        client=coordinated_client,
    )
    runtime = SerialDashboardRuntime(state=dashboard_state)
    platform_config = PlatformConfig(data_dir=resolved_database.path.parent)
    platform_config.ensure_directories()
    resolved_maps = map_repository or MapRepository(
        database=resolved_database,
        artifacts_dir=platform_config.artifacts_dir,
    )
    resolved_physical_profiles = (
        physical_profile_repository
        or PhysicalProfileRepository(database=resolved_database)
    )
    resolved_physical_profiles.sync_from_yaml()
    resolved_event_store = (
        task_orchestrator.event_store
        if task_orchestrator is not None
        else EventStore(
            database=resolved_database,
            runs_dir=platform_config.runs_dir,
        )
    )
    resolved_scoring = scoring_service or ScoringService(
        database=resolved_database,
        runs_dir=platform_config.runs_dir,
    )
    resolved_replay = replay_service or ReplayService(
        database=resolved_database,
        event_store=resolved_event_store,
        data_dir=platform_config.data_dir,
    )
    resolved_retention = retention_manager or RetentionManager(
        database=resolved_database,
        data_dir=platform_config.data_dir,
    )
    run_finalizer = RunFinalizer(
        scoring=resolved_scoring,
        replay=resolved_replay,
        retention=resolved_retention,
        event_store=resolved_event_store,
    )
    if task_orchestrator is None:
        simulation_adapter = (
            SimulationModeAdapter(
                session_factory=lambda _endpoint: coordinated_client,
                map_provider=resolved_maps.get_version,
                physical_profile_provider=(
                    resolved_physical_profiles.get
                ),
            )
            if coordinated_client is not None
            and client_mode == "simulation"
            else SimulationModeAdapter(
                map_provider=resolved_maps.get_version,
                physical_profile_provider=(
                    resolved_physical_profiles.get
                ),
            )
        )
        adapters = {
            "simulation": simulation_adapter,
            "real": RealModeAdapter(),
        }

        def runner_factory(task: TaskRecord) -> MazeRunner:
            adapter = adapters[task.mode]
            if not isinstance(adapter, SimulationModeAdapter):
                raise RuntimeError(
                    "real task execution requires the future RDK X3 Agent"
                )
            map_version = resolved_maps.get_version(task.map_version)
            maze = MazeMap.from_definition(
                map_version.definition,
                wall_threshold_mm=int(
                    dashboard_state.params.get("tof.wall_threshold_mm")
                ),
                map_version_id=map_version.version_id,
            )
            dashboard_state.set_maze(maze)
            return MazeRunner(
                client=adapter.session,
                params=dashboard_state.params,
                maze=maze,
                planner=MazePlanner(),
                action_prefix=task.run_id or task.task_id,
            )

        resolved_tasks = TaskOrchestrator(
            database=resolved_database,
            event_store=resolved_event_store,
            adapters=adapters,
            runner_factory=runner_factory,
            run_finalizer=run_finalizer,
            physical_profile_repository=resolved_physical_profiles,
        )
    else:
        resolved_tasks = task_orchestrator
        if resolved_tasks.run_finalizer is None:
            resolved_tasks.run_finalizer = run_finalizer
    dashboard_state.attach_task_orchestrator(resolved_tasks)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await runtime.start()
        try:
            yield
        finally:
            resolved_tasks.close()
            await runtime.stop()

    app = FastAPI(title="RDK Maze Tuner", version="0.1.0", lifespan=lifespan)
    app.state.database = resolved_database
    app.state.auth = resolved_auth
    app.state.control_lease = resolved_leases
    app.state.dashboard = dashboard_state
    app.state.runtime = runtime
    app.state.task_orchestrator = resolved_tasks
    app.state.map_repository = resolved_maps
    app.state.physical_profile_repository = (
        resolved_physical_profiles
    )
    app.state.scoring_service = resolved_scoring
    app.state.replay_service = resolved_replay
    app.state.retention_manager = resolved_retention
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(create_auth_router(auth_context))
    app.include_router(create_control_router(auth_context, resolved_leases))
    app.include_router(
        create_tasks_router(auth_context, resolved_leases, resolved_tasks)
    )
    app.include_router(
        create_maps_router(auth_context, resolved_leases, resolved_maps)
    )
    app.include_router(
        create_physical_profiles_router(
            auth_context,
            resolved_physical_profiles,
        )
    )
    app.include_router(create_runs_router(auth_context, resolved_replay))

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((TEMPLATE_DIR / "index.html").read_text(encoding="utf-8"))

    @app.get("/api/health")
    def api_health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "maze-dashboard",
        }

    @app.get("/api/state")
    def api_state(request: Request) -> dict[str, Any]:
        principal = auth_context.require_principal(request)
        return _state_payload(dashboard_state, resolved_leases, principal)

    @app.post("/api/params")
    async def api_params(request: Request) -> dict[str, Any]:
        principal = auth_context.require_state_change(request)
        _require_control(request, resolved_leases, principal)
        body = await _json_body(request)
        updates = body.get("updates")
        try:
            result = dashboard_state.apply_param_updates(updates)
            resolved_leases.audit_operation(
                principal,
                "apply_param",
                details={"parameter_names": sorted((updates or {}).keys())},
            )
            return result
        except ParamValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SerialClientError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/command/estop")
    async def api_estop(request: Request) -> dict[str, Any]:
        principal = auth_context.require_state_change(request)
        body = await _json_body(request)
        reason = str(body.get("reason") or "dashboard")
        try:
            owner = resolved_tasks.command_owner()
            if owner is None:
                result = dashboard_state.estop(reason=reason)
                routed_to = "device"
            else:
                task = resolved_tasks.estop(owner["task_id"])
                result = {
                    "ok": True,
                    "routed_to": "task",
                    "task": task,
                }
                routed_to = "task"
            resolved_leases.audit_operation(
                principal,
                "estop",
                details={
                    "reason_provided": bool(reason),
                    "routed_to": routed_to,
                },
            )
            return result
        except TaskConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ModeAdapterError, SerialClientError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/command/stop")
    async def api_stop(request: Request) -> dict[str, Any]:
        principal = auth_context.require_state_change(request)
        _require_control(request, resolved_leases, principal)
        body = await _json_body(request)
        reason = str(body.get("reason") or "dashboard")
        try:
            owner = resolved_tasks.command_owner()
            if owner is None:
                result = dashboard_state.stop(reason=reason)
                routed_to = "device"
            else:
                task = resolved_tasks.stop(owner["task_id"])
                result = {
                    "ok": True,
                    "routed_to": "task",
                    "task": task,
                }
                routed_to = "task"
            resolved_leases.audit_operation(
                principal,
                "stop",
                details={
                    "reason_provided": bool(reason),
                    "routed_to": routed_to,
                },
            )
            return result
        except TaskConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ModeAdapterError, SerialClientError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/command/action")
    async def api_action(request: Request) -> dict[str, Any]:
        principal = auth_context.require_state_change(request)
        _require_control(request, resolved_leases, principal)
        body = await _json_body(request)
        name = str(body.get("name") or "")
        if name not in {"move_cell", "turn_left", "turn_right", "turn_back"}:
            raise HTTPException(status_code=400, detail="unknown action")
        owner = resolved_tasks.command_owner()
        if owner is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "manual action is blocked while active task "
                    f"{owner['task_id']} owns motion commands"
                ),
            )
        result = dashboard_state.manual_action(name=name)
        resolved_leases.audit_operation(
            principal,
            "action",
            details={"name": name, "action_id": result.get("action_id")},
        )
        return result

    @app.post("/api/auto-tune")
    async def api_auto_tune(request: Request) -> dict[str, Any]:
        principal = auth_context.require_state_change(request)
        _require_control(request, resolved_leases, principal)
        body = await _json_body(request)
        enabled = bool(body.get("enabled"))
        result = dashboard_state.set_auto_tune(enabled)
        resolved_leases.audit_operation(
            principal,
            "auto_tune",
            details={"enabled": enabled},
        )
        return result

    @app.websocket("/ws")
    async def websocket_state(websocket: WebSocket) -> None:
        try:
            principal = auth_context.websocket_principal(websocket)
        except HTTPException:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "state",
                "payload": _state_payload(
                    dashboard_state,
                    resolved_leases,
                    principal,
                ),
            }
        )
        try:
            while True:
                message = await websocket.receive_json()
                if message.get("type") == "ping":
                    await websocket.send_json(
                        {
                            "type": "pong",
                            "payload": _state_payload(
                                dashboard_state,
                                resolved_leases,
                                principal,
                            ),
                        }
                    )
                else:
                    await websocket.send_json(
                        {
                            "type": "state",
                            "payload": _state_payload(
                                dashboard_state,
                                resolved_leases,
                                principal,
                            ),
                        }
                    )
        except WebSocketDisconnect:
            return

    return app


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _require_control(
    request: Request,
    leases: ControlLeaseService,
    principal: SessionPrincipal,
) -> None:
    try:
        leases.require_holder(
            principal,
            request.headers.get(CONTROL_LEASE_HEADER_NAME),
        )
    except LeasePermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _state_payload(
    dashboard_state: DashboardState,
    leases: ControlLeaseService,
    principal: SessionPrincipal,
) -> dict[str, Any]:
    payload = dashboard_state.snapshot()
    payload["auth"] = {
        "user": {
            "user_id": principal.user_id,
            "username": principal.username,
        },
        "control": leases.status_for(principal),
    }
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RDK X3 maze tuning dashboard")
    parser.add_argument("--host", default="0.0.0.0", help="Dashboard bind host")
    parser.add_argument("--port", type=int, default=8000, help="Dashboard bind port")
    parser.add_argument("--params", type=Path, default=DEFAULT_PARAMS, help="Path to params.yaml")
    parser.add_argument("--limits", type=Path, default=DEFAULT_LIMITS, help="Path to limits.yaml")
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument("--serial", help="Optional ESP32 serial port")
    transport.add_argument("--tcp", metavar="HOST:PORT", help="Optional Webots simulation endpoint")
    parser.add_argument("--baud", type=int, default=115200, help="Serial baud rate")
    parser.add_argument("--timeout", type=float, default=3.0, help="Protocol wait timeout in seconds")
    return parser


def run(args: argparse.Namespace) -> int:
    client = None
    if args.tcp:
        stream = open_tcp(args.tcp)
        client = SerialClient(stream, timeout_s=args.timeout)
    elif args.serial:
        stream = open_serial(args.serial, baud=args.baud)
        client = SerialClient(stream, timeout_s=args.timeout)

    import uvicorn

    uvicorn.run(
        create_app(
            params_path=args.params,
            limits_path=args.limits,
            client=client,
            client_mode=(
                "simulation"
                if args.tcp
                else "real"
                if args.serial
                else None
            ),
        ),
        host=args.host,
        port=args.port,
    )
    return 0


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
