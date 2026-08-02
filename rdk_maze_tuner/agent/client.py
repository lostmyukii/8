"""Outbound WSS client that keeps public latency outside the action loop."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Mapping

from .config import AgentConfig
from .runtime import AgentRuntime, AgentRuntimeState


class AgentClient:
    def __init__(
        self,
        *,
        config: AgentConfig,
        runtime: AgentRuntime,
    ) -> None:
        self.config = config
        self.runtime = runtime
        self._closed = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._seen_message_ids: set[str] = set()

    async def run_forever(self) -> None:
        delay = self.config.reconnect_initial_s
        while not self._closed.is_set():
            try:
                await self._run_connection()
                delay = self.config.reconnect_initial_s
            except asyncio.CancelledError:
                raise
            except Exception:
                if self.runtime.state not in {
                    AgentRuntimeState.IDLE,
                    AgentRuntimeState.LOST,
                    AgentRuntimeState.COMPLETED,
                    AgentRuntimeState.ESTOP,
                }:
                    await asyncio.to_thread(
                        self.runtime.on_cloud_disconnect
                    )
            if self._closed.is_set():
                break
            try:
                await asyncio.wait_for(
                    self._closed.wait(),
                    timeout=delay,
                )
            except TimeoutError:
                pass
            delay = min(
                self.config.reconnect_max_s,
                delay * 2.0,
            )

    async def close(self) -> None:
        self._closed.set()
        task = self._task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await asyncio.to_thread(self.runtime.close)

    async def _run_connection(self) -> None:
        try:
            from websockets.asyncio.client import connect
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "websockets dependency is required for the RDK Agent"
            ) from exc

        loop = asyncio.get_running_loop()
        outbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=256
        )

        def emit(message: dict[str, Any]) -> None:
            def enqueue() -> None:
                try:
                    outbound.put_nowait(message)
                except asyncio.QueueFull:
                    if self.runtime.state is AgentRuntimeState.RUNNING:
                        asyncio.create_task(
                            asyncio.to_thread(
                                self.runtime.on_cloud_disconnect
                            )
                        )

            loop.call_soon_threadsafe(enqueue)

        self.runtime.event_sink = emit
        async with connect(
            self.config.server_url,
            ssl=self.config.ssl_context(),
            additional_headers={
                "Authorization": (
                    f"Bearer {self.config.device_token}"
                )
            },
            ping_interval=20,
            ping_timeout=20,
            max_size=2 * 1024 * 1024,
        ) as websocket:
            await websocket.send(
                _json(
                    {
                        "type": "agent.hello",
                        "schema_version": 1,
                        "payload": {
                            "device_id": self.config.device_id,
                            "runtime_state": self.runtime.state.value,
                            "features": [
                                "map_goal_navigation",
                                "motion_evidence",
                                "bounded_recovery",
                            ],
                        },
                    }
                )
            )
            sender = asyncio.create_task(
                self._send_loop(websocket, outbound)
            )
            heartbeat = asyncio.create_task(
                self._heartbeat_loop(outbound)
            )
            try:
                async for raw in websocket:
                    message = _parse(raw)
                    await self._handle(message)
            finally:
                sender.cancel()
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sender
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
                if (
                    not self._closed.is_set()
                    and self.runtime.state
                    in {
                        AgentRuntimeState.READY,
                        AgentRuntimeState.RUNNING,
                    }
                ):
                    await asyncio.to_thread(
                        self.runtime.on_cloud_disconnect
                    )

    async def _handle(self, message: Mapping[str, Any]) -> None:
        message_type = str(message.get("type") or "")
        if message_type == "agent.welcome":
            return
        message_id = str(message.get("message_id") or "")
        if message_id:
            if message_id in self._seen_message_ids:
                return
            self._seen_message_ids.add(message_id)
            if len(self._seen_message_ids) > 1024:
                self._seen_message_ids = set(
                    sorted(self._seen_message_ids)[-512:]
                )
        if message_type == "task.prepare":
            await asyncio.to_thread(self.runtime.prepare, message)
            return
        if message_type == "task.start":
            if self._task is not None and not self._task.done():
                return
            self._task = asyncio.create_task(
                asyncio.to_thread(self.runtime.run)
            )
            return
        if message_type == "task.pause":
            await asyncio.to_thread(
                self.runtime.stop,
                reason="pause requested",
            )
            return
        if message_type == "task.stop":
            await asyncio.to_thread(
                self.runtime.stop,
                reason="server stop",
            )
            return
        if message_type == "task.estop":
            await asyncio.to_thread(
                self.runtime.estop,
                reason="dashboard",
            )
            return
        if message_type == "task.clear_estop":
            await asyncio.to_thread(self.runtime.clear_estop)
            return
        raise RuntimeError(
            f"server sent unsupported task-level message: {message_type}"
        )

    @staticmethod
    async def _send_loop(websocket, outbound) -> None:
        while True:
            message = await outbound.get()
            await websocket.send(_json(message))

    async def _heartbeat_loop(self, outbound) -> None:
        while True:
            await asyncio.sleep(10.0)
            await outbound.put(
                {
                    "type": "agent.heartbeat",
                    "schema_version": 1,
                    "payload": {
                        "device_id": self.config.device_id,
                        "runtime_state": self.runtime.state.value,
                    },
                }
            )


def _json(value: Mapping[str, Any]) -> str:
    import json

    return json.dumps(
        dict(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _parse(value: str | bytes) -> dict[str, Any]:
    import json

    if isinstance(value, bytes):
        value = value.decode("utf-8")
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise RuntimeError("Agent message must be an object")
    return payload
