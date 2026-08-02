"""Executable entry point for the RDK X3 maze Agent."""

from __future__ import annotations

import asyncio

from rdk_maze_tuner.core.device_session import DeviceSession
from rdk_maze_tuner.core.serial_client import SerialClient, open_serial

from .client import AgentClient
from .config import AgentConfig
from .runtime import AgentRuntime


async def run(config: AgentConfig) -> None:
    stream = open_serial(
        config.serial_port,
        baud=config.serial_baud,
    )
    serial = SerialClient(
        stream,
        timeout_s=config.serial_timeout_s,
    )
    session = DeviceSession(
        serial,
        action_result_timeout_s=max(
            10.0,
            config.serial_timeout_s,
        ),
    )
    runtime = AgentRuntime(
        session=session,
        event_sink=lambda _message: None,
    )
    client = AgentClient(config=config, runtime=runtime)
    try:
        await client.run_forever()
    finally:
        await client.close()


def main() -> int:
    asyncio.run(run(AgentConfig.from_env()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
