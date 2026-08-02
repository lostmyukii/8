from __future__ import annotations

import asyncio
import ssl
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from rdk_maze_tuner.agent.config import AgentConfig
from rdk_maze_tuner.agent.client import AgentClient
from rdk_maze_tuner.agent.runtime import (
    AgentEnvelopeError,
    build_task_envelope,
    validate_task_envelope,
)
from rdk_maze_tuner.core.maze_definition import (
    MapDefinition,
    StartPose,
    WallSegment,
)
from rdk_maze_tuner.core.motion_evidence import (
    ArrivalVerificationConfig,
)
from rdk_maze_tuner.dashboard.app import create_app
from rdk_maze_tuner.platform.agent_registry import (
    AgentProtocolError,
    AgentRegistry,
)
from rdk_maze_tuner.platform.database import Database
from rdk_maze_tuner.platform.device_tokens import DeviceTokenService
from rdk_maze_tuner.platform.modes.real import RealModeAdapter
from rdk_maze_tuner.platform.task_state import TaskStatus


def map_definition():
    rows = cols = 5
    return MapDefinition(
        rows=rows,
        cols=cols,
        cell_width_mm=250,
        cell_height_mm=250,
        wall_thickness_mm=40,
        wall_height_mm=180,
        start=StartPose(0, 4, "N"),
        goals=((4, 0),),
        walls=(
            *(WallSegment(x, 0, x + 1, 0) for x in range(cols)),
            *(WallSegment(x, rows, x + 1, rows) for x in range(cols)),
            *(WallSegment(0, y, 0, y + 1) for y in range(rows)),
            *(WallSegment(cols, y, cols, y + 1) for y in range(rows)),
        ),
    )


def envelope():
    definition = map_definition()
    return build_task_envelope(
        task_id="task-real-1",
        run_id="run-real-1",
        map_version_id="map-v2",
        map_digest=definition.content_digest,
        map_definition=definition.to_dict(),
        goal={
            "type": "map_goal",
            "cell": [4, 0],
            "source_map_version": "map-v2",
            "source_map_digest": definition.content_digest,
        },
        param_version_id="param-v1",
        param_digest="a" * 64,
        param_snapshot={"motor": {"base_speed": 0.25}},
        arrival_verification=ArrivalVerificationConfig().to_dict(),
        max_steps=50,
    )


def test_task_envelope_freezes_map_goal_params_and_completion_thresholds():
    payload = envelope()

    assert payload["type"] == "task.prepare"
    assert payload["map"]["version_id"] == "map-v2"
    assert payload["map"]["digest"] == map_definition().content_digest
    assert payload["goal"]["cell"] == [4, 0]
    assert payload["params"] == {
        "version_id": "param-v1",
        "digest": "a" * 64,
        "snapshot": {"motor": {"base_speed": 0.25}},
    }
    assert payload["completion"]["goal_min_confidence"] == 0.8
    assert validate_task_envelope(payload)["task_id"] == "task-real-1"


@pytest.mark.parametrize("field", ["digest", "goal"])
def test_agent_refuses_map_digest_or_primary_goal_mismatch(field):
    payload = envelope()
    if field == "digest":
        payload["map"]["digest"] = "0" * 64
    else:
        payload["goal"]["cell"] = [3, 0]

    with pytest.raises(AgentEnvelopeError):
        validate_task_envelope(payload)


def test_agent_wss_config_always_verifies_system_ca_and_hostname():
    config = AgentConfig(
        server_url="wss://maze.example.test/ws/agents/rdk-x3-a",
        device_id="rdk-x3-a",
        device_token="secret-token",
        serial_port="serial-path-from-environment",
    )

    context = config.ssl_context()

    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert "ssl_no_verify" not in AgentConfig.__dataclass_fields__
    with pytest.raises(ValueError, match="wss"):
        AgentConfig(
            server_url="ws://maze.example.test/ws/agents/rdk-x3-a",
            device_id="rdk-x3-a",
            device_token="secret-token",
            serial_port="serial-path-from-environment",
        )


class NoControl:
    def stop_requested(self):
        return False


def test_real_mode_preflight_and_task_delivery_require_authenticated_agent(
    tmp_path,
):
    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()
    tokens = DeviceTokenService(database=database)
    issued = tokens.register(device_id="rdk-x3-a", name="maze-rdk")
    registry = AgentRegistry()
    definition = map_definition()
    param = {
        "version_id": "param-v1",
        "digest": "c" * 64,
        "snapshot": {"motor": {"base_speed": 0.25}},
    }
    adapter = RealModeAdapter(
        registry=registry,
        device_id="rdk-x3-a",
        map_provider=lambda _version: {
            "version_id": "map-v2",
            "digest": definition.content_digest,
            "definition": definition.to_dict(),
        },
        param_provider=lambda _version: param,
    )

    assert adapter.preflight()["code"] == "DEVICE_OFFLINE"

    principal = tokens.authenticate("rdk-x3-a", issued["token"])
    connection = registry.connect(principal)
    assert adapter.preflight()["ok"] is True
    adapter.reset(map_version="map-v2", param_version="param-v1")
    task = SimpleNamespace(
        task_id="task-real-1",
        run_id="run-real-1",
        map_version="map-v2",
        param_version="param-v1",
        goal={
            "type": "map_goal",
            "cell": [4, 0],
            "source_map_version": "map-v2",
            "source_map_digest": definition.content_digest,
        },
        arrival_verification_snapshot=(
            ArrivalVerificationConfig().to_dict()
        ),
        max_steps=50,
    )

    runner = adapter.prepare_task(task)
    prepared = connection.next_outbound()

    assert prepared["type"] == "task.prepare"
    assert prepared["message_id"].startswith("server-")
    assert prepared["goal"]["cell"] == [4, 0]
    assert "left_pwm" not in str(prepared)
    with pytest.raises(AgentProtocolError, match="left_pwm"):
        connection.send(
            {
                "type": "task.start",
                "task_id": "task-real-1",
                "left_pwm": 100,
            }
        )

    connection.receive(
        {
            "type": "task.result",
            "payload": {
                "task_id": "task-real-1",
                "run_id": "run-real-1",
                "status": "COMPLETED",
                "result": "goal_verified",
                "pose": {"grid_cell": [4, 0], "heading": "E"},
            },
        }
    )
    terminal = runner.run_step(
        control=NoControl(),
        goal=lambda _maze, _telemetry: False,
        event_sink=lambda _event: None,
    )
    assert terminal.outcome == "goal_verified"


class CountingRuntime:
    def __init__(self):
        self.prepare_calls = 0

    def prepare(self, _message):
        self.prepare_calls += 1


def test_agent_deduplicates_replayed_server_message_ids():
    runtime = CountingRuntime()
    client = AgentClient(
        config=AgentConfig(
            server_url=(
                "wss://maze.example.test/ws/agents/rdk-x3-a"
            ),
            device_id="rdk-x3-a",
            device_token="test-only-device-token",
            serial_port="serial-path-from-environment",
        ),
        runtime=runtime,
    )
    message = {**envelope(), "message_id": "server-fixed-1"}

    asyncio.run(client._handle(message))
    asyncio.run(client._handle(message))

    assert runtime.prepare_calls == 1


def test_local_websocket_carries_whole_real_task_without_motor_commands(
    tmp_path,
):
    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()
    tokens = DeviceTokenService(database=database)
    issued = tokens.register(device_id="rdk-x3-a", name="maze-rdk")
    registry = AgentRegistry()
    app = create_app(
        database=database,
        device_token_service=tokens,
        agent_registry=registry,
    )
    definition = map_definition()
    _map, version = app.state.map_repository.create_map(
        name="agent integration",
        definition=definition.to_dict(),
        created_by_user_id=None,
    )

    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/ws/agents/rdk-x3-a",
                headers={"Authorization": "Bearer wrong-token"},
            ):
                pass

        with client.websocket_connect(
            "/ws/agents/rdk-x3-a",
            headers={
                "Authorization": f"Bearer {issued['token']}"
            },
        ) as socket:
            assert socket.receive_json()["type"] == "agent.welcome"
            orchestrator = app.state.task_orchestrator
            task = orchestrator.create_task(
                mode="real",
                map_version=version.version_id,
                param_version="1",
                max_steps=50,
            )
            assert orchestrator.preflight(task["task_id"])["status"] == (
                "PREFLIGHT"
            )
            ready = orchestrator.reset(task["task_id"])
            prepared = socket.receive_json()

            assert ready["status"] == "READY"
            assert prepared["type"] == "task.prepare"
            assert prepared["map"]["digest"] == version.digest
            assert prepared["goal"]["cell"] == [4, 0]
            assert "left_pwm" not in str(prepared).lower()
            assert "right_pwm" not in str(prepared).lower()
            assert prepared["type"] != "action"

            orchestrator.start(task["task_id"])
            start = socket.receive_json()
            assert start["type"] == "task.start"
            socket.send_json(
                {
                    "type": "task.status",
                    "payload": {
                        "task_id": task["task_id"],
                        "run_id": ready["run_id"],
                        "status": "RUNNING",
                    },
                }
            )
            socket.send_json(
                {
                    "type": "task.result",
                    "payload": {
                        "task_id": task["task_id"],
                        "run_id": ready["run_id"],
                        "status": "COMPLETED",
                        "result": "goal_verified",
                        "pose": {
                            "grid_cell": [4, 0],
                            "heading": "E",
                            "confidence": 0.95,
                        },
                        "verification": {"verified": True},
                    },
                }
            )
            completed = orchestrator.wait_for_state(
                task["task_id"],
                {TaskStatus.COMPLETED},
                timeout_s=2.0,
            )

            assert completed["status"] == "COMPLETED"
