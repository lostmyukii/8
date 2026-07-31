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

from rdk_maze_tuner.core.param_manager import ParamManager, ParamValidationError
from rdk_maze_tuner.core.serial_client import SerialClient, SerialClientError, open_serial
from rdk_maze_tuner.core.tcp_stream import open_tcp
from rdk_maze_tuner.dashboard.runtime import SerialDashboardRuntime
from rdk_maze_tuner.dashboard.state import DashboardState


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
    client: Optional[SerialClient] = None,
    state: Optional[DashboardState] = None,
) -> FastAPI:
    params = ParamManager(params_path=params_path, limits_path=limits_path)
    dashboard_state = state or DashboardState(params=params, client=client)
    runtime = SerialDashboardRuntime(state=dashboard_state)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="RDK Maze Tuner", version="0.1.0", lifespan=lifespan)
    app.state.dashboard = dashboard_state
    app.state.runtime = runtime
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((TEMPLATE_DIR / "index.html").read_text(encoding="utf-8"))

    @app.get("/api/state")
    def api_state() -> dict[str, Any]:
        return dashboard_state.snapshot()

    @app.post("/api/params")
    async def api_params(request: Request) -> dict[str, Any]:
        body = await _json_body(request)
        updates = body.get("updates")
        try:
            return dashboard_state.apply_param_updates(updates)
        except ParamValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SerialClientError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/command/estop")
    async def api_estop(request: Request) -> dict[str, Any]:
        body = await _json_body(request)
        reason = str(body.get("reason") or "dashboard")
        try:
            return dashboard_state.estop(reason=reason)
        except SerialClientError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/command/stop")
    async def api_stop(request: Request) -> dict[str, Any]:
        body = await _json_body(request)
        reason = str(body.get("reason") or "dashboard")
        try:
            return dashboard_state.stop(reason=reason)
        except SerialClientError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/command/action")
    async def api_action(request: Request) -> dict[str, Any]:
        body = await _json_body(request)
        name = str(body.get("name") or "")
        if name not in {"move_cell", "turn_left", "turn_right", "turn_back"}:
            raise HTTPException(status_code=400, detail="unknown action")
        return dashboard_state.manual_action(name=name)

    @app.post("/api/auto-tune")
    async def api_auto_tune(request: Request) -> dict[str, Any]:
        body = await _json_body(request)
        return dashboard_state.set_auto_tune(bool(body.get("enabled")))

    @app.websocket("/ws")
    async def websocket_state(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_json({"type": "state", "payload": dashboard_state.snapshot()})
        try:
            while True:
                message = await websocket.receive_json()
                if message.get("type") == "ping":
                    await websocket.send_json({"type": "pong", "payload": dashboard_state.snapshot()})
                else:
                    await websocket.send_json({"type": "state", "payload": dashboard_state.snapshot()})
        except WebSocketDisconnect:
            return

    return app


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


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
        create_app(params_path=args.params, limits_path=args.limits, client=client),
        host=args.host,
        port=args.port,
    )
    return 0


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
