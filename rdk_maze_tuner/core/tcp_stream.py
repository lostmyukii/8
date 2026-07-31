"""TCP byte stream adapter for the newline-delimited ESP32 protocol."""

from __future__ import annotations

import socket
from typing import Tuple


class TcpStreamError(RuntimeError):
    """Raised when a TCP simulation endpoint is invalid or disconnects."""


def parse_tcp_endpoint(endpoint: str) -> Tuple[str, int]:
    """Parse ``host:port`` or ``[IPv6]:port`` into a socket address."""
    value = endpoint.strip()
    if value.startswith("["):
        closing = value.find("]")
        if closing <= 1 or closing + 1 >= len(value) or value[closing + 1] != ":":
            raise TcpStreamError("TCP endpoint must use [IPv6]:port")
        host = value[1:closing]
        port_text = value[closing + 2 :]
    else:
        host, separator, port_text = value.rpartition(":")
        if not separator or not host:
            raise TcpStreamError("TCP endpoint must use host:port")

    try:
        port = int(port_text)
    except ValueError as exc:
        raise TcpStreamError("TCP endpoint port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise TcpStreamError("TCP endpoint port must be between 1 and 65535")
    return host, port


class SocketJsonStream:
    """Small serial-like wrapper around a connected TCP socket."""

    def __init__(self, sock: socket.socket) -> None:
        self._socket = sock
        self._buffer = bytearray()
        self._closed = False

    def write(self, data: bytes) -> int:
        if self._closed:
            raise TcpStreamError("TCP stream is closed")
        self._socket.sendall(data)
        return len(data)

    def flush(self) -> None:
        return None

    def readline(self) -> bytes:
        if self._closed:
            return b""
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._buffer[: newline + 1])
                del self._buffer[: newline + 1]
                return line
            try:
                chunk = self._socket.recv(4096)
            except socket.timeout:
                return b""
            if not chunk:
                self.close()
                raise TcpStreamError("TCP simulation endpoint disconnected")
            self._buffer.extend(chunk)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._socket.close()


def open_tcp(
    endpoint: str,
    *,
    read_timeout_s: float = 0.1,
    connect_timeout_s: float = 5.0,
) -> SocketJsonStream:
    """Connect to a TCP simulation endpoint and return a serial-like stream."""
    host, port = parse_tcp_endpoint(endpoint)
    try:
        sock = socket.create_connection((host, port), timeout=connect_timeout_s)
    except OSError as exc:
        raise TcpStreamError(f"cannot connect to TCP simulation endpoint {endpoint}: {exc}") from exc
    sock.settimeout(read_timeout_s)
    return SocketJsonStream(sock)
