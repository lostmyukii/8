"""Synchronous serial client for the ESP32 newline JSON protocol."""

from __future__ import annotations

import time
from threading import Lock
from typing import Any, Callable, Dict, Mapping, Optional, Protocol

from .protocol import (
    Message,
    build_action,
    build_estop,
    build_heartbeat,
    build_set_params,
    build_stop,
    decode_line,
    encode_message,
)


class SerialLike(Protocol):
    def write(self, data: bytes) -> int:
        ...

    def flush(self) -> Any:
        ...

    def readline(self) -> bytes:
        ...


class SerialClientError(RuntimeError):
    """Raised when ESP32 reports an error or rejects a command."""


class TimeoutError(SerialClientError):
    """Raised when the ESP32 does not answer in time."""


class SerialClient:
    def __init__(
        self,
        stream: SerialLike,
        *,
        timeout_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.stream = stream
        self.timeout_s = timeout_s
        self._clock = clock
        self._sleep = sleep
        self._seq = 0
        self._seq_lock = Lock()
        self._write_lock = Lock()
        self._reader_lock = Lock()
        self._reader_owner: object | None = None
        self.last_telemetry: Optional[Message] = None

    def wait_ready(self, *, timeout_s: Optional[float] = None) -> Message:
        return self._wait_for_type("ready", timeout_s=timeout_s)

    def wait_telemetry(self, *, timeout_s: Optional[float] = None) -> Message:
        return self._wait_for_type("telemetry", timeout_s=timeout_s)

    def send_heartbeat(self, *, ts_ms: Optional[int] = None) -> Message:
        seq = self._next_seq()
        if ts_ms is None:
            ts_ms = int(time.time() * 1000)
        self._send(build_heartbeat(seq=seq, ts_ms=ts_ms))
        return self._wait_for_ack(seq)

    def send_params(self, params: Mapping[str, Any]) -> Message:
        seq = self._next_seq()
        self._send(build_set_params(seq=seq, params=params))
        return self._wait_for_ack(seq)

    def execute_action(self, *, action_id: str, name: str, speed: float, target_ticks: int) -> Message:
        _ack, result = self.execute_action_with_ack(
            action_id=action_id,
            name=name,
            speed=speed,
            target_ticks=target_ticks,
        )
        if result.get("type") == "done":
            if result.get("success") is False:
                raise SerialClientError(f"action {action_id} returned unsuccessful done")
            return result
        code = result.get("code") or "ESP32_ERROR"
        detail = result.get("message") or ""
        raise SerialClientError(f"{code}: {detail}".strip())

    def execute_action_with_ack(self, *, action_id: str, name: str, speed: float, target_ticks: int) -> tuple[Message, Message]:
        seq = self._next_seq()
        self._send(build_action(seq=seq, action_id=action_id, name=name, speed=speed, target_ticks=target_ticks))
        ack = self._wait_for_ack(seq)
        result = self._wait_for_action_result_message(action_id)
        return ack, result

    def stop(self) -> Message:
        seq = self._next_seq()
        self._send(build_stop(seq=seq))
        return self._wait_for_ack(seq)

    def estop(self, *, reason: str = "rdk") -> Message:
        seq = self._next_seq()
        self._send(build_estop(seq=seq, reason=reason))
        return self._wait_for_ack(seq)

    def claim_reader(self, owner: object) -> None:
        """Give one coordinator exclusive ownership of transport reads."""
        with self._reader_lock:
            if self._reader_owner is not None and self._reader_owner is not owner:
                raise SerialClientError("transport reader is already owned by another DeviceSession")
            self._reader_owner = owner

    def release_reader(self, owner: object) -> None:
        with self._reader_lock:
            if self._reader_owner is owner:
                self._reader_owner = None

    def read_message(self, *, owner: object | None = None) -> Optional[Message]:
        with self._reader_lock:
            if self._reader_owner is not None and self._reader_owner is not owner:
                raise SerialClientError("transport reader is owned by DeviceSession")
        line = self.stream.readline()
        if not line:
            return None
        message = decode_line(line)
        if message.get("type") == "telemetry":
            self.last_telemetry = message
        return message

    def send_message(self, message: Mapping[str, Any]) -> None:
        """Write one frame without reading its response."""
        payload = encode_message(message)
        with self._write_lock:
            self.stream.write(payload)
            self.stream.flush()

    def next_seq(self) -> int:
        """Reserve a command sequence number safely across caller threads."""
        with self._seq_lock:
            self._seq += 1
            return self._seq

    def _send(self, message: Mapping[str, Any]) -> None:
        self.send_message(message)

    def _next_seq(self) -> int:
        return self.next_seq()

    def _wait_for_ack(self, seq: int) -> Message:
        deadline = self._clock() + self.timeout_s
        while self._clock() <= deadline:
            message = self.read_message()
            if message is None:
                self._sleep(0.001)
                continue
            if message.get("type") != "ack" or message.get("seq") != seq:
                self._raise_if_global_error(message)
                continue
            if message.get("ok") is not True:
                raise SerialClientError(str(message.get("message") or f"command seq {seq} rejected"))
            return message
        raise TimeoutError(f"timeout waiting for ack seq {seq}")

    def _wait_for_type(self, expected_type: str, *, timeout_s: Optional[float] = None) -> Message:
        timeout = self.timeout_s if timeout_s is None else timeout_s
        deadline = self._clock() + timeout
        while self._clock() <= deadline:
            message = self.read_message()
            if message is None:
                self._sleep(0.001)
                continue
            if message.get("type") == expected_type:
                return message
            self._raise_if_global_error(message)
        raise TimeoutError(f"timeout waiting for {expected_type}")

    def _wait_for_action_result_message(self, action_id: str) -> Message:
        deadline = self._clock() + self.timeout_s
        while self._clock() <= deadline:
            message = self.read_message()
            if message is None:
                self._sleep(0.001)
                continue
            if message.get("action_id") != action_id:
                self._raise_if_global_error(message)
                continue
            if message.get("type") == "done":
                return message
            if message.get("type") == "error":
                return message
        raise TimeoutError(f"timeout waiting for result of action {action_id}")

    def _raise_if_global_error(self, message: Message) -> None:
        if message.get("type") == "error" and "action_id" not in message:
            code = message.get("code") or "ESP32_ERROR"
            detail = message.get("message") or ""
            raise SerialClientError(f"{code}: {detail}".strip())


def open_serial(port: str, *, baud: int = 115200, timeout_s: float = 0.1) -> SerialLike:
    try:
        import serial  # type: ignore
    except ModuleNotFoundError as exc:
        raise SerialClientError("pyserial is required for real serial ports; install with `python3 -m pip install pyserial`") from exc
    return serial.Serial(port=port, baudrate=baud, timeout=timeout_s)
