from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Callable
from typing import Protocol

from lan_input_share.elevated.authorization import authorize_client
from lan_input_share.elevated.models import (
    HELPER_PROTOCOL_VERSION,
    ApprovedClient,
    ClientIdentity,
    HelperState,
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
from lan_input_share.input.events import InputEvent
from lan_input_share.input.key_state import PressedInputState
from lan_input_share.windows.process_identity import (
    default_input_desktop_name,
    inspect_process_identity,
)


class HelperConnection(Protocol):
    client_pid: int

    def read(self, size: int = 0) -> bytes: ...

    def write(self, data: bytes) -> None: ...

    def close(self) -> None: ...


class HelperPipeServer(Protocol):
    def accept(self) -> HelperConnection: ...

    def close(self) -> None: ...


class InputInjector(Protocol):
    def inject(self, event: InputEvent) -> None: ...

    def release_all(self, events: tuple[InputEvent, ...]) -> None: ...


_ReadResult = bytes | BaseException | None


class ElevatedInputHelper:
    def __init__(
        self,
        approved: ApprovedClient,
        injector: InputInjector,
        pipe_server: HelperPipeServer,
        *,
        heartbeat_timeout: float = 3.0,
        helper_session_id: int | None = None,
        identity_inspector: Callable[[int], ClientIdentity] = inspect_process_identity,
        desktop_name: Callable[[], str | None] = default_input_desktop_name,
    ) -> None:
        if heartbeat_timeout <= 0:
            raise ValueError("心跳超时必须大于零")
        self._approved = approved
        self._injector = injector
        self._pipe_server = pipe_server
        self._heartbeat_timeout = heartbeat_timeout
        self._identity_inspector = identity_inspector
        self._desktop_name = desktop_name
        self._helper_session_id = (
            helper_session_id
            if helper_session_id is not None
            else inspect_process_identity(os.getpid()).session_id
        )
        self._stopped = threading.Event()

    def run(self) -> int:
        while not self._stopped.is_set():
            try:
                connection = self._pipe_server.accept()
            except BaseException:
                if self._stopped.is_set():
                    break
                continue
            self.serve_connection(connection)
        return 0

    def stop(self) -> None:
        self._stopped.set()
        self._pipe_server.close()

    def serve_connection(self, connection: HelperConnection) -> None:
        pressed = PressedInputState()
        try:
            try:
                actual = self._identity_inspector(connection.client_pid)
            except (OSError, RuntimeError, ValueError):
                return
            decision = authorize_client(
                self._approved,
                actual,
                self._helper_session_id,
            )
            if not decision.allowed:
                return
            self._serve_authorized(connection, pressed)
        except (ConnectionError, OSError, RuntimeError, HelperProtocolError):
            return
        finally:
            self._release_pressed(pressed)
            connection.close()

    def _serve_authorized(
        self,
        connection: HelperConnection,
        pressed: PressedInputState,
    ) -> None:
        available = getattr(connection, "available", None)
        if callable(available):
            self._serve_polled(connection, pressed, available)
            return
        incoming: queue.Queue[_ReadResult] = queue.Queue()
        reader = threading.Thread(
            target=self._read_connection,
            args=(connection, incoming),
            name="LanInputShareHelperReader",
            daemon=True,
        )
        reader.start()
        decoder = HelperFrameDecoder()
        hello_received = False
        last_contact = time.monotonic()
        status_sequence = 0
        current_state = HelperState.SECURE_DESKTOP

        while not self._stopped.is_set():
            remaining = self._heartbeat_timeout - (time.monotonic() - last_contact)
            if remaining <= 0:
                return
            try:
                item = incoming.get(timeout=remaining)
            except queue.Empty:
                return
            if item is None:
                return
            if isinstance(item, BaseException):
                raise ConnectionError("助手命名管道读取失败") from item
            messages = decoder.feed(item)
            for message in messages:
                last_contact = time.monotonic()
                if not hello_received:
                    if not isinstance(message, Hello):
                        raise HelperProtocolError("首条助手消息必须是 hello")
                    if (
                        message.protocol_version != HELPER_PROTOCOL_VERSION
                        or message.client_pid != connection.client_pid
                    ):
                        raise HelperProtocolError("助手 hello 身份或版本不一致")
                    hello_received = True
                    current_state = self._write_status(connection, status_sequence)
                    status_sequence += 1
                    continue
                if isinstance(message, Hello | Status):
                    raise HelperProtocolError("客户端发送了不允许的助手消息")
                if isinstance(message, Input):
                    if current_state is HelperState.RUNNING:
                        self._injector.inject(message.event)
                        pressed.apply(message.event)
                elif isinstance(message, ReleaseAll):
                    self._release_pressed(pressed)
                elif isinstance(message, Ping):
                    current_state = self._write_status(connection, status_sequence)
                    status_sequence += 1
                elif isinstance(message, ShutdownIfIdle) and not pressed.release_events():
                    return

    def _serve_polled(
        self,
        connection: HelperConnection,
        pressed: PressedInputState,
        available: Callable[[], int],
    ) -> None:
        decoder = HelperFrameDecoder()
        hello_received = False
        last_contact = time.monotonic()
        status_sequence = 0
        current_state = HelperState.SECURE_DESKTOP
        while not self._stopped.is_set():
            remaining = self._heartbeat_timeout - (time.monotonic() - last_contact)
            if remaining <= 0:
                return
            if available() <= 0:
                time.sleep(min(0.002, remaining))
                continue
            data = connection.read()
            if not data:
                return
            for message in decoder.feed(data):
                last_contact = time.monotonic()
                if not hello_received:
                    if not isinstance(message, Hello):
                        raise HelperProtocolError("首条助手消息必须是 hello")
                    if (
                        message.protocol_version != HELPER_PROTOCOL_VERSION
                        or message.client_pid != connection.client_pid
                    ):
                        raise HelperProtocolError("助手 hello 身份或版本不一致")
                    hello_received = True
                    current_state = self._write_status(connection, status_sequence)
                    status_sequence += 1
                    continue
                if isinstance(message, Hello | Status):
                    raise HelperProtocolError("客户端发送了不允许的助手消息")
                if isinstance(message, Input):
                    if current_state is HelperState.RUNNING:
                        self._injector.inject(message.event)
                        pressed.apply(message.event)
                elif isinstance(message, ReleaseAll):
                    self._release_pressed(pressed)
                elif isinstance(message, Ping):
                    current_state = self._write_status(connection, status_sequence)
                    status_sequence += 1
                elif isinstance(message, ShutdownIfIdle) and not pressed.release_events():
                    return

    @staticmethod
    def _read_connection(
        connection: HelperConnection,
        incoming: queue.Queue[_ReadResult],
    ) -> None:
        try:
            while True:
                data = connection.read()
                if not data:
                    incoming.put(None)
                    return
                incoming.put(data)
        except BaseException as exc:
            incoming.put(exc)

    def _write_status(
        self, connection: HelperConnection, sequence: int
    ) -> HelperState:
        state = self._current_state()
        detail = (
            "高权限输入助手可用"
            if state is HelperState.RUNNING
            else "当前为 Windows 安全桌面，已暂停远程输入"
        )
        connection.write(encode_helper_message(Status(sequence, state, detail)))
        return state

    def _current_state(self) -> HelperState:
        name = self._desktop_name()
        if name is not None and name.casefold() == "default":
            return HelperState.RUNNING
        return HelperState.SECURE_DESKTOP

    def _release_pressed(self, pressed: PressedInputState) -> None:
        releases = pressed.release_events()
        if releases:
            try:
                self._injector.release_all(releases)
            finally:
                pressed.clear()


__all__ = ["ElevatedInputHelper"]
