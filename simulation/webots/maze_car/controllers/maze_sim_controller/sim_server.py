"""Single-client localhost TCP server for the simulated ESP32 protocol."""

from __future__ import annotations

import json
import socket
from typing import Any

from simulation.webots.maze_car.engine_contract import (
    SimulationProtocolEngine,
)


class SimProtocolServer:
    def __init__(
        self,
        engine: SimulationProtocolEngine,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        self.engine = engine
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((host, port))
        self.listener.listen(1)
        self.listener.setblocking(False)
        self.client: socket.socket | None = None
        self.buffer = bytearray()
        self._now_ms = 0
        self._closed = False

    def poll(self, *, now_ms: int) -> None:
        if self._closed:
            return
        self._now_ms = now_ms
        self._accept_client(now_ms=now_ms)
        if self.client is None:
            self.engine.tick(now_ms=now_ms)
            return
        self._read_messages(now_ms=now_ms)
        self._send_many(self.engine.tick(now_ms=now_ms))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._drop_client(now_ms=self._now_ms)
        finally:
            try:
                self.listener.close()
            finally:
                self.engine.close()

    def _accept_client(self, *, now_ms: int) -> None:
        if self.client is not None:
            return
        try:
            client, _address = self.listener.accept()
        except BlockingIOError:
            return
        client.settimeout(0.0)
        self.client = client
        self.buffer.clear()
        self.engine.on_client_connected(now_ms=now_ms)
        self._send_many(
            [
                self.engine.ready_message(),
                self.engine.telemetry_message(),
            ]
        )

    def _read_messages(self, *, now_ms: int) -> None:
        if self.client is None:
            return
        while True:
            try:
                chunk = self.client.recv(4096)
            except (BlockingIOError, socket.timeout):
                break
            except OSError:
                self._drop_client(now_ms=now_ms)
                return
            if not chunk:
                self._drop_client(now_ms=now_ms)
                return
            self.buffer.extend(chunk)

        while True:
            newline = self.buffer.find(b"\n")
            if newline < 0:
                break
            raw = bytes(self.buffer[:newline]).strip()
            del self.buffer[: newline + 1]
            if not raw:
                continue
            try:
                message = json.loads(raw.decode("utf-8"))
                if not isinstance(message, dict):
                    raise ValueError("payload is not an object")
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                self._send_many(
                    [
                        {
                            "type": "error",
                            "code": "INVALID_JSON",
                            "message": str(exc),
                            "simulated": True,
                        }
                    ]
                )
                continue
            self._send_many(self.engine.handle(message, now_ms=now_ms))

    def _send_many(self, messages: list[dict[str, Any]]) -> None:
        if self.client is None:
            return
        try:
            for message in messages:
                payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
                self.client.sendall(payload.encode("utf-8") + b"\n")
        except OSError:
            self._drop_client(now_ms=self._now_ms)

    def _drop_client(self, *, now_ms: int) -> None:
        if self.client is None:
            return
        try:
            self.client.close()
        finally:
            self.client = None
            self.buffer.clear()
            self.engine.on_client_disconnected(now_ms=now_ms)
