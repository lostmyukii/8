"""Background serial runtime for the dashboard."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, Optional

from rdk_maze_tuner.core.device_session import DeviceDisconnectedError
from rdk_maze_tuner.dashboard.state import DashboardState


class SerialDashboardRuntime:
    def __init__(
        self,
        *,
        state: DashboardState,
        poll_interval_s: float = 0.05,
        heartbeat_interval_s: float = 0.3,
    ) -> None:
        self.state = state
        self.poll_interval_s = poll_interval_s
        self.heartbeat_interval_s = heartbeat_interval_s
        self._poll_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._subscription: Any = None

    async def start(self) -> None:
        if self.state.client is None or self._poll_task is not None:
            return
        self._ensure_subscription()
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        tasks = [task for task in (self._poll_task, self._heartbeat_task) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._poll_task = None
        self._heartbeat_task = None
        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None
        if self.state.client is not None:
            self.state.client.close()

    def poll_once(self) -> Optional[dict]:
        subscription = self._ensure_subscription()
        if subscription is None:
            return None
        try:
            message = subscription.get(timeout_s=0.0)
        except DeviceDisconnectedError as exc:
            return self.state.handle_device_disconnect(str(exc))
        if message is None:
            return None
        return self.state.handle_device_message(message)

    def send_heartbeat_once(self) -> dict:
        return self.state.send_heartbeat()

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.to_thread(self.poll_once)
            await asyncio.sleep(self.poll_interval_s)

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.send_heartbeat_once)
            except DeviceDisconnectedError as exc:
                self.state.handle_device_disconnect(str(exc))
                return
            await asyncio.sleep(self.heartbeat_interval_s)

    def _ensure_subscription(self) -> Any:
        if self._subscription is not None:
            return self._subscription
        client = self.state.client
        if client is None:
            return None
        client.start()
        self._subscription = client.subscribe(
            message_types={"ready", "telemetry"}
        )
        return self._subscription
