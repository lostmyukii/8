"""Background serial runtime for the dashboard."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Optional

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

    async def start(self) -> None:
        if not self.state.connected or self._poll_task is not None:
            return
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

    def poll_once(self) -> Optional[dict]:
        return self.state.read_serial_once()

    def send_heartbeat_once(self) -> dict:
        return self.state.send_heartbeat()

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.to_thread(self.poll_once)
            await asyncio.sleep(self.poll_interval_s)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.to_thread(self.send_heartbeat_once)
            await asyncio.sleep(self.heartbeat_interval_s)
