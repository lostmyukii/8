"""Single-reader device session for serial and simulation transports."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from .protocol import (
    Message,
    build_action,
    build_estop,
    build_heartbeat,
    build_set_params,
    build_stop,
)
from .serial_client import (
    SerialClient,
    SerialClientError,
    TimeoutError as SerialTimeoutError,
)


class DeviceSessionError(SerialClientError):
    """Base error for a coordinated device session."""


class DeviceSessionTimeout(SerialTimeoutError, DeviceSessionError):
    """Raised when a routed response does not arrive in time."""


class DeviceDisconnectedError(DeviceSessionError):
    """Raised for all pending operations after the transport disconnects."""


@dataclass
class _Pending:
    response: queue.Queue[Message | BaseException]

    @classmethod
    def create(cls) -> "_Pending":
        return cls(response=queue.Queue(maxsize=1))

    def resolve(self, value: Message | BaseException) -> None:
        try:
            self.response.put_nowait(value)
        except queue.Full:
            return

    def wait(self, *, timeout_s: float, description: str) -> Message:
        try:
            value = self.response.get(timeout=timeout_s)
        except queue.Empty as exc:
            raise DeviceSessionTimeout(f"timeout waiting for {description}") from exc
        if isinstance(value, BaseException):
            raise value
        return value


class DeviceSubscription:
    """Bounded message subscription fed by the session reader."""

    def __init__(
        self,
        session: "DeviceSession",
        subscription_id: int,
        *,
        message_types: frozenset[str] | None,
        max_queue: int,
    ) -> None:
        self._session = session
        self._subscription_id = subscription_id
        self._message_types = message_types
        self._queue: queue.Queue[Message | BaseException] = queue.Queue(
            maxsize=max_queue
        )
        self._closed = False

    def accepts(self, message: Mapping[str, Any]) -> bool:
        return (
            self._message_types is None
            or str(message.get("type") or "") in self._message_types
        )

    def publish(self, value: Message | BaseException) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(value)
            return
        except queue.Full:
            pass
        try:
            self._queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(value)
        except queue.Full:
            pass

    def get(self, *, timeout_s: float = 0.0) -> Optional[Message]:
        if self._closed:
            return None
        try:
            value = self._queue.get(timeout=max(0.0, timeout_s))
        except queue.Empty:
            return None
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._session._remove_subscription(self._subscription_id)

    def __enter__(self) -> "DeviceSubscription":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class DeviceSession:
    """Own the sole transport reader and route messages to matching waiters."""

    def __init__(
        self,
        client: SerialClient,
        *,
        idle_sleep_s: float = 0.002,
    ) -> None:
        self.client = client
        self.timeout_s = client.timeout_s
        self._idle_sleep_s = idle_sleep_s
        self._reader_owner = object()
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._stopped = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._ack_waiters: dict[int, _Pending] = {}
        self._action_waiters: dict[str, _Pending] = {}
        self._type_waiters: dict[str, list[_Pending]] = {}
        self._subscriptions: dict[int, DeviceSubscription] = {}
        self._next_subscription_id = 0
        self._connected = False
        self._failure: DeviceDisconnectedError | None = None
        self._ready: Message | None = None
        self._last_telemetry: Message | None = None

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected and self._failure is None

    @property
    def last_telemetry(self) -> Message | None:
        with self._lock:
            return (
                None
                if self._last_telemetry is None
                else dict(self._last_telemetry)
            )

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._reader_thread is not None and self._reader_thread.is_alive():
                return
            if self._stopped.is_set():
                raise DeviceDisconnectedError("device session is closed")
            with self._lock:
                self._raise_if_failed_locked()
            self.client.claim_reader(self._reader_owner)
            with self._lock:
                self._connected = True
            self._reader_thread = threading.Thread(
                target=self._reader_loop,
                name="device-session-reader",
                daemon=True,
            )
            self._reader_thread.start()

    def close(self) -> None:
        self._stopped.set()
        self._fail(DeviceDisconnectedError("device session closed"))
        stream_close = getattr(self.client.stream, "close", None)
        if callable(stream_close):
            try:
                stream_close()
            except Exception:
                pass
        thread = self._reader_thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=1.0)
        self.client.release_reader(self._reader_owner)

    def subscribe(
        self,
        *,
        message_types: Iterable[str] | None = None,
        max_queue: int = 128,
    ) -> DeviceSubscription:
        if max_queue < 1:
            raise ValueError("max_queue must be at least 1")
        with self._lock:
            self._next_subscription_id += 1
            subscription = DeviceSubscription(
                self,
                self._next_subscription_id,
                message_types=(
                    None
                    if message_types is None
                    else frozenset(str(item) for item in message_types)
                ),
                max_queue=max_queue,
            )
            self._subscriptions[self._next_subscription_id] = subscription
            return subscription

    def wait_ready(self, *, timeout_s: Optional[float] = None) -> Message:
        with self._lock:
            self._raise_if_failed_locked()
            if self._ready is not None:
                return dict(self._ready)
        return self._wait_for_type("ready", timeout_s=timeout_s)

    def wait_telemetry(self, *, timeout_s: Optional[float] = None) -> Message:
        with self._lock:
            self._raise_if_failed_locked()
            if self._last_telemetry is not None:
                return dict(self._last_telemetry)
        return self._wait_for_type("telemetry", timeout_s=timeout_s)

    def send_heartbeat(self, *, ts_ms: Optional[int] = None) -> Message:
        if ts_ms is None:
            ts_ms = int(time.time() * 1000)
        seq = self.client.next_seq()
        return self._send_and_wait_for_ack(
            build_heartbeat(seq=seq, ts_ms=ts_ms),
            seq=seq,
        )

    def send_params(self, params: Mapping[str, Any]) -> Message:
        seq = self.client.next_seq()
        return self._send_and_wait_for_ack(
            build_set_params(seq=seq, params=params),
            seq=seq,
        )

    def execute_action(
        self,
        *,
        action_id: str,
        name: str,
        speed: float,
        target_ticks: int,
    ) -> Message:
        _ack, result = self.execute_action_with_ack(
            action_id=action_id,
            name=name,
            speed=speed,
            target_ticks=target_ticks,
        )
        if result.get("type") == "done":
            if result.get("success") is False:
                raise SerialClientError(
                    f"action {action_id} returned unsuccessful done"
                )
            return result
        code = result.get("code") or "ESP32_ERROR"
        detail = result.get("message") or ""
        raise SerialClientError(f"{code}: {detail}".strip())

    def execute_action_with_ack(
        self,
        *,
        action_id: str,
        name: str,
        speed: float,
        target_ticks: int,
    ) -> tuple[Message, Message]:
        self._ensure_started()
        seq = self.client.next_seq()
        ack_pending = _Pending.create()
        result_pending = _Pending.create()
        with self._lock:
            self._raise_if_failed_locked()
            if action_id in self._action_waiters:
                raise DeviceSessionError(
                    f"action_id is already pending: {action_id}"
                )
            self._ack_waiters[seq] = ack_pending
            self._action_waiters[action_id] = result_pending
        try:
            self._write(
                build_action(
                    seq=seq,
                    action_id=action_id,
                    name=name,
                    speed=speed,
                    target_ticks=target_ticks,
                )
            )
            ack = ack_pending.wait(
                timeout_s=self.timeout_s,
                description=f"ack seq {seq}",
            )
            self._validate_ack(ack, seq=seq)
            result = result_pending.wait(
                timeout_s=self.timeout_s,
                description=f"result of action {action_id}",
            )
            return ack, result
        finally:
            with self._lock:
                self._ack_waiters.pop(seq, None)
                self._action_waiters.pop(action_id, None)

    def stop(self) -> Message:
        seq = self.client.next_seq()
        return self._send_and_wait_for_ack(build_stop(seq=seq), seq=seq)

    def estop(self, *, reason: str = "rdk") -> Message:
        seq = self.client.next_seq()
        return self._send_and_wait_for_ack(
            build_estop(seq=seq, reason=reason),
            seq=seq,
        )

    def request_ack(self, message_type: str, **fields: Any) -> Message:
        if not message_type:
            raise ValueError("message_type is required")
        seq = self.client.next_seq()
        message = {"type": message_type, "seq": seq, **fields}
        return self._send_and_wait_for_ack(message, seq=seq)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "connected": self._connected and self._failure is None,
                "ready": None if self._ready is None else dict(self._ready),
                "telemetry": (
                    None
                    if self._last_telemetry is None
                    else dict(self._last_telemetry)
                ),
                "last_error": (
                    None if self._failure is None else str(self._failure)
                ),
            }

    def _send_and_wait_for_ack(
        self,
        message: Mapping[str, Any],
        *,
        seq: int,
    ) -> Message:
        self._ensure_started()
        pending = _Pending.create()
        with self._lock:
            self._raise_if_failed_locked()
            self._ack_waiters[seq] = pending
        try:
            self._write(message)
            ack = pending.wait(
                timeout_s=self.timeout_s,
                description=f"ack seq {seq}",
            )
            self._validate_ack(ack, seq=seq)
            return ack
        finally:
            with self._lock:
                self._ack_waiters.pop(seq, None)

    def _wait_for_type(
        self,
        message_type: str,
        *,
        timeout_s: Optional[float],
    ) -> Message:
        self._ensure_started()
        pending = _Pending.create()
        with self._lock:
            self._raise_if_failed_locked()
            self._type_waiters.setdefault(message_type, []).append(pending)
        try:
            return pending.wait(
                timeout_s=self.timeout_s if timeout_s is None else timeout_s,
                description=message_type,
            )
        finally:
            with self._lock:
                waiters = self._type_waiters.get(message_type, [])
                if pending in waiters:
                    waiters.remove(pending)
                if not waiters:
                    self._type_waiters.pop(message_type, None)

    def _ensure_started(self) -> None:
        if self._reader_thread is None or not self._reader_thread.is_alive():
            self.start()

    def _write(self, message: Mapping[str, Any]) -> None:
        try:
            self.client.send_message(message)
        except BaseException as exc:
            failure = DeviceDisconnectedError(
                f"device disconnected while writing: {exc}"
            )
            self._fail(failure)
            raise failure from exc

    def _reader_loop(self) -> None:
        try:
            while not self._stopped.is_set():
                message = self.client.read_message(owner=self._reader_owner)
                if message is None:
                    self._stopped.wait(self._idle_sleep_s)
                    continue
                self._dispatch(message)
        except BaseException as exc:
            if self._stopped.is_set():
                return
            self._fail(
                DeviceDisconnectedError(f"device disconnected while reading: {exc}")
            )
        finally:
            with self._lock:
                self._connected = False

    def _dispatch(self, message: Message) -> None:
        message_type = str(message.get("type") or "")
        pending: _Pending | None = None
        type_waiters: list[_Pending] = []
        subscriptions: list[DeviceSubscription] = []
        with self._lock:
            if message_type == "ready":
                self._ready = dict(message)
            elif message_type == "telemetry":
                self._last_telemetry = dict(message)
                self.client.last_telemetry = dict(message)

            if message_type == "ack":
                seq = message.get("seq")
                if isinstance(seq, int):
                    pending = self._ack_waiters.get(seq)
            elif message_type in {"done", "error"}:
                action_id = message.get("action_id")
                if isinstance(action_id, str):
                    pending = self._action_waiters.get(action_id)

            type_waiters = list(self._type_waiters.pop(message_type, []))
            subscriptions = list(self._subscriptions.values())

        if pending is not None:
            pending.resolve(dict(message))
        for waiter in type_waiters:
            waiter.resolve(dict(message))
        for subscription in subscriptions:
            if subscription.accepts(message):
                subscription.publish(dict(message))

    def _fail(self, failure: DeviceDisconnectedError) -> None:
        waiters: list[_Pending] = []
        subscriptions: list[DeviceSubscription] = []
        with self._lock:
            if self._failure is not None:
                return
            self._failure = failure
            self._connected = False
            waiters.extend(self._ack_waiters.values())
            waiters.extend(self._action_waiters.values())
            for values in self._type_waiters.values():
                waiters.extend(values)
            subscriptions = list(self._subscriptions.values())
        for waiter in waiters:
            waiter.resolve(failure)
        for subscription in subscriptions:
            subscription.publish(failure)

    def _raise_if_failed_locked(self) -> None:
        if self._failure is not None:
            raise self._failure

    def _remove_subscription(self, subscription_id: int) -> None:
        with self._lock:
            self._subscriptions.pop(subscription_id, None)

    @staticmethod
    def _validate_ack(ack: Mapping[str, Any], *, seq: int) -> None:
        if ack.get("type") != "ack" or ack.get("seq") != seq:
            raise DeviceSessionError(f"invalid ack for seq {seq}")
        if ack.get("ok") is not True:
            raise SerialClientError(
                str(ack.get("message") or f"command seq {seq} rejected")
            )
