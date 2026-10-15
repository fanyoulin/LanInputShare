from __future__ import annotations

import os
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from typing import Protocol, cast

from lan_input_share.elevated.models import ApprovedClient, HelperState, HelperStatus
from lan_input_share.elevated.pipe_transport import (
    NamedPipeClient,
    pipe_name_for,
)
from lan_input_share.elevated.protocol import (
    Hello,
    HelperFrameDecoder,
    HelperProtocolError,
    Input,
    Ping,
    ReleaseAll,
    ShutdownIfIdle,
    Status,
    encode_helper_message,
)
from lan_input_share.input.events import InputEvent, MouseMoveEvent
from lan_input_share.windows.process_identity import inspect_process_identity


class HelperUnavailableError(RuntimeError):
    pass


class HelperQueueFull(HelperUnavailableError):
    pass


class BridgeConnection(Protocol):
    def read(self, size: int = 0) -> bytes: ...

    def write(self, data: bytes) -> None: ...

    def close(self) -> None: ...


class BridgePipeClient(Protocol):
    def connect(self, timeout: float) -> BridgeConnection: ...


_ReleaseMarker = type[ReleaseAll]
_QueueItem = InputEvent | _ReleaseMarker


class _InputQueue:
    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("助手输入队列容量必须大于零")
        self._capacity = capacity
        self._items: deque[_QueueItem] = deque()
        self._condition = threading.Condition()

    def put_event(self, event: InputEvent) -> None:
        with self._condition:
            if isinstance(event, MouseMoveEvent) and self._replace_last_motion(event):
                self._condition.notify()
                return
            if len(self._items) >= self._capacity:
                if isinstance(event, MouseMoveEvent):
                    return
                if not self._discard_last_motion():
                    raise HelperQueueFull("高权限输入助手队列已满")
            self._items.append(event)
            self._condition.notify()

    def put_release(self) -> None:
        with self._condition:
            self._items.clear()
            self._items.append(ReleaseAll)
            self._condition.notify()

    def get(self, timeout: float | None) -> _QueueItem | None:
        with self._condition:
            if not self._items:
                self._condition.wait(timeout)
            if not self._items:
                return None
            return self._items.popleft()

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def _replace_last_motion(self, event: MouseMoveEvent) -> bool:
        if self._items and isinstance(self._items[-1], MouseMoveEvent):
            self._items[-1] = event
            return True
        return False

    def _discard_last_motion(self) -> bool:
        for index in range(len(self._items) - 1, -1, -1):
            if isinstance(self._items[index], MouseMoveEvent):
                del self._items[index]
                return True
        return False


def reconnect_delays() -> Iterator[float]:
    delay = 0.5
    while True:
        yield delay
        delay = min(delay * 2, 5.0)


class HelperInputBridge:
    def __init__(
        self,
        approved: ApprovedClient,
        *,
        capacity: int = 1024,
        heartbeat_interval: float = 1.0,
        pipe_client: BridgePipeClient | None = None,
        process_id: int | None = None,
        connect_timeout: float = 0.5,
        initial_status: HelperStatus | None = None,
        installation_status: Callable[[], HelperStatus] | None = None,
        ensure_started: Callable[[], None] | None = None,
    ) -> None:
        if heartbeat_interval <= 0:
            raise ValueError("助手心跳间隔必须大于零")
        if connect_timeout < 0:
            raise ValueError("助手连接超时不能为负数")
        pid = process_id if process_id is not None else os.getpid()
        if pipe_client is None:
            session_id = inspect_process_identity(pid).session_id
            pipe_client = NamedPipeClient(pipe_name_for(approved.user_sid, session_id))
        self._approved = approved
        self._pipe_client = pipe_client
        self._process_id = pid
        self._heartbeat_interval = heartbeat_interval
        self._connect_timeout = connect_timeout
        self._installation_status = installation_status
        self._ensure_started = ensure_started
        self._queue = _InputQueue(capacity)
        self._status = initial_status or HelperStatus(
            HelperState.UNAVAILABLE,
            "高权限输入助手未连接",
        )
        self._status_lock = threading.Lock()
        self._callbacks: list[Callable[[HelperStatus], None]] = []
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._connection: BridgeConnection | None = None
        self._connection_lock = threading.Lock()

    @property
    def status(self) -> HelperStatus:
        with self._status_lock:
            return self._status

    def add_status_callback(self, callback: Callable[[HelperStatus], None]) -> None:
        with self._status_lock:
            self._callbacks.append(callback)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopped.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="LanInputShareHelperBridge",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._queue.wake()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(2.0, self._heartbeat_interval + 1.0))
        with self._connection_lock:
            connection = self._connection
        if connection is not None:
            connection.close()
        self._thread = None

    def inject(self, event: InputEvent) -> None:
        if self.status.state is not HelperState.RUNNING:
            raise HelperUnavailableError(self.status.detail)
        try:
            self._queue.put_event(event)
        except HelperQueueFull:
            self._set_status(HelperState.UNAVAILABLE, "高权限输入助手队列已满")
            raise

    def release_all(self, events: tuple[InputEvent, ...]) -> None:
        del events
        if self.status.state in {HelperState.RUNNING, HelperState.SECURE_DESKTOP}:
            self._queue.put_release()

    def _run(self) -> None:
        delays = reconnect_delays()
        while not self._stopped.is_set():
            try:
                if self._installation_status is not None:
                    installation = self._installation_status()
                    if installation.state is not HelperState.RUNNING:
                        self._set_status(installation.state, installation.detail)
                        if not self._stopped.is_set():
                            self._stopped.wait(next(delays))
                        continue
                if self._ensure_started is not None:
                    self._ensure_started()
                connection = self._pipe_client.connect(self._connect_timeout)
                with self._connection_lock:
                    self._connection = connection
                self._run_connection(connection)
                delays = reconnect_delays()
                if not self._stopped.is_set():
                    self._queue.put_release()
                    self._set_status(
                        HelperState.UNAVAILABLE,
                        "高权限输入助手连接已断开，正在重连",
                    )
            except Exception as exc:
                self._set_status(HelperState.UNAVAILABLE, f"高权限输入助手不可用：{exc}")
            finally:
                with self._connection_lock:
                    active_connection = self._connection
                    self._connection = None
                if active_connection is not None:
                    active_connection.close()
            if not self._stopped.is_set():
                self._stopped.wait(next(delays))
        self._set_status(HelperState.UNAVAILABLE, "高权限输入助手已停止")

    def _run_connection(self, connection: BridgeConnection) -> None:
        sequence = 0
        connection.write(
            encode_helper_message(
                Hello(sequence, self._approved.protocol_version, self._process_id)
            )
        )
        sequence += 1
        available = getattr(connection, "available", None)
        if callable(available):
            self._run_polled_connection(connection, available, sequence)
            return
        disconnected = threading.Event()
        reader = threading.Thread(
            target=self._read_statuses,
            args=(connection, disconnected),
            name="LanInputShareHelperStatusReader",
            daemon=True,
        )
        reader.start()
        next_heartbeat = time.monotonic() + self._heartbeat_interval
        try:
            while not self._stopped.is_set() and not disconnected.is_set():
                timeout = max(0.0, next_heartbeat - time.monotonic())
                item = self._queue.get(timeout)
                if item is ReleaseAll:
                    connection.write(encode_helper_message(ReleaseAll(sequence)))
                    sequence += 1
                elif item is not None:
                    connection.write(
                        encode_helper_message(Input(sequence, cast(InputEvent, item)))
                    )
                    sequence += 1
                if time.monotonic() >= next_heartbeat:
                    connection.write(encode_helper_message(Ping(sequence)))
                    sequence += 1
                    next_heartbeat = time.monotonic() + self._heartbeat_interval
            if self._stopped.is_set():
                connection.write(encode_helper_message(ShutdownIfIdle(sequence)))
        finally:
            connection.close()
            reader.join(timeout=1.0)

    def _run_polled_connection(
        self,
        connection: BridgeConnection,
        available: Callable[[], int],
        sequence: int,
    ) -> None:
        decoder = HelperFrameDecoder()
        next_heartbeat = time.monotonic() + self._heartbeat_interval
        while not self._stopped.is_set():
            item = self._queue.get(
                min(0.002, max(0.0, next_heartbeat - time.monotonic()))
            )
            if item is ReleaseAll:
                connection.write(encode_helper_message(ReleaseAll(sequence)))
                sequence += 1
            elif item is not None:
                connection.write(
                    encode_helper_message(Input(sequence, cast(InputEvent, item)))
                )
                sequence += 1
            if time.monotonic() >= next_heartbeat:
                connection.write(encode_helper_message(Ping(sequence)))
                sequence += 1
                next_heartbeat = time.monotonic() + self._heartbeat_interval
            if available() > 0:
                data = connection.read()
                if not data:
                    raise ConnectionError("高权限输入助手已断开")
                for message in decoder.feed(data):
                    if not isinstance(message, Status):
                        raise HelperProtocolError("助手返回了非状态消息")
                    self._set_status(message.state, message.detail)
        connection.write(encode_helper_message(ShutdownIfIdle(sequence)))

    def _read_statuses(
        self,
        connection: BridgeConnection,
        disconnected: threading.Event,
    ) -> None:
        decoder = HelperFrameDecoder()
        try:
            while not self._stopped.is_set():
                data = connection.read()
                if not data:
                    return
                for message in decoder.feed(data):
                    if not isinstance(message, Status):
                        raise HelperProtocolError("助手返回了非状态消息")
                    self._set_status(message.state, message.detail)
        except (ConnectionError, OSError, RuntimeError, HelperProtocolError):
            return
        finally:
            disconnected.set()
            self._queue.wake()

    def _set_status(self, state: HelperState, detail: str) -> None:
        status = HelperStatus(state, detail)
        with self._status_lock:
            if status == self._status:
                return
            self._status = status
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            try:
                callback(status)
            except Exception:
                continue


__all__ = [
    "HelperInputBridge",
    "HelperQueueFull",
    "HelperUnavailableError",
    "reconnect_delays",
]
