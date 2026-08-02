"""Environment-only RDK Agent configuration with mandatory TLS checks."""

from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse


@dataclass(frozen=True)
class AgentConfig:
    server_url: str
    device_id: str
    device_token: str
    serial_port: str
    serial_baud: int = 115_200
    serial_timeout_s: float = 3.0
    reconnect_initial_s: float = 1.0
    reconnect_max_s: float = 30.0

    def __post_init__(self) -> None:
        parsed = urlparse(self.server_url)
        if (
            parsed.scheme != "wss"
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError(
                "server_url must be a credential-free wss URL"
            )
        expected_suffix = f"/ws/agents/{self.device_id}"
        if parsed.path.rstrip("/") != expected_suffix:
            raise ValueError(
                "server_url path must end with "
                f"{expected_suffix}"
            )
        for name in ("device_id", "device_token", "serial_port"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} is required")
        if self.serial_baud <= 0 or self.serial_timeout_s <= 0:
            raise ValueError("serial settings must be positive")
        if (
            self.reconnect_initial_s <= 0
            or self.reconnect_max_s < self.reconnect_initial_s
        ):
            raise ValueError("invalid reconnect backoff")

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> "AgentConfig":
        values = os.environ if env is None else env
        required = {
            "server_url": values.get("MAZE_AGENT_SERVER_URL", ""),
            "device_id": values.get("MAZE_AGENT_DEVICE_ID", ""),
            "device_token": values.get("MAZE_AGENT_DEVICE_TOKEN", ""),
            "serial_port": values.get("MAZE_AGENT_SERIAL_PORT", ""),
        }
        missing = [
            name
            for name, value in required.items()
            if not str(value).strip()
        ]
        if missing:
            raise ValueError(
                "missing Agent environment values: "
                + ", ".join(sorted(missing))
            )
        return cls(
            **required,
            serial_baud=int(
                values.get("MAZE_AGENT_SERIAL_BAUD", "115200")
            ),
            serial_timeout_s=float(
                values.get("MAZE_AGENT_SERIAL_TIMEOUT_S", "3")
            ),
        )

    def ssl_context(self) -> ssl.SSLContext:
        """Return the system-CA context; no insecure mode exists."""

        context = ssl.create_default_context()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return context
