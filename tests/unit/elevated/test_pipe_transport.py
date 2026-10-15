from __future__ import annotations

import threading

import pytest

from lan_input_share.elevated.pipe_transport import (
    NamedPipeClient,
    NamedPipeServer,
    PipeClosedError,
    pipe_name_for,
    pipe_sddl_for,
)


class FakeBackend:
    def __init__(self) -> None:
        self.handles: list[object] = []
        self.closed: list[object] = []
        self.cancelled: list[object] = []
        self.writes: list[bytes] = []
        self.read_result = b"reply"
        self.client_pid = 4321
        self.connect_entered = threading.Event()
        self.connect_release = threading.Event()
        self.read_entered = threading.Event()
        self.read_release = threading.Event()

    def create_server(self, name: str, sddl: str) -> object:
        handle = object()
        self.handles.append(handle)
        return handle

    def connect_server(self, handle: object) -> None:
        self.connect_entered.set()
        self.connect_release.wait(timeout=2)
        if handle in self.cancelled:
            raise PipeClosedError("closed")

    def open_client(self, name: str, timeout: float) -> object:
        handle = object()
        self.handles.append(handle)
        return handle

    def read(self, handle: object, size: int) -> bytes:
        self.read_entered.set()
        self.read_release.wait(timeout=2)
        if handle in self.cancelled:
            raise PipeClosedError("closed")
        return self.read_result

    def write(self, handle: object, data: bytes) -> None:
        self.writes.append(data)

    def cancel(self, handle: object) -> None:
        self.cancelled.append(handle)
        self.connect_release.set()
        self.read_release.set()

    def close(self, handle: object) -> None:
        self.closed.append(handle)

    def get_client_pid(self, handle: object) -> int:
        return self.client_pid


def test_pipe_name_is_scoped_to_normalized_sid_and_session() -> None:
    assert pipe_name_for(" s-1-5-21-1000 ", 2) == (
        r"\\.\pipe\LanInputShare.Input.S-1-5-21-1000.2"
    )


def test_pipe_name_rejects_invalid_session() -> None:
    with pytest.raises(ValueError, match="会话"):
        pipe_name_for("S-1-5-21-1000", -1)


def test_pipe_sddl_grants_only_user_and_system() -> None:
    sddl = pipe_sddl_for("S-1-5-21-1000")

    assert sddl == "D:P(A;;GA;;;SY)(A;;GA;;;S-1-5-21-1000)"
    assert "WD" not in sddl
    assert "AU" not in sddl


def test_server_exposes_client_pid_before_any_read() -> None:
    backend = FakeBackend()
    backend.connect_release.set()
    server = NamedPipeServer("pipe", "S-1-5-21-1000", backend=backend)

    connection = server.accept()

    assert connection.client_pid == 4321
    assert backend.read_entered.is_set() is False
    connection.close()
    server.close()


def test_closing_server_unblocks_blocked_accept() -> None:
    backend = FakeBackend()
    server = NamedPipeServer("pipe", "S-1-5-21-1000", backend=backend)
    errors: list[BaseException] = []
    thread = threading.Thread(target=lambda: _capture_error(server.accept, errors))
    thread.start()
    assert backend.connect_entered.wait(timeout=1)

    server.close()
    thread.join(timeout=1)

    assert thread.is_alive() is False
    assert isinstance(errors[0], PipeClosedError)


def test_closing_connection_unblocks_blocked_read() -> None:
    backend = FakeBackend()
    connection = NamedPipeClient("pipe", backend=backend).connect(timeout=0.5)
    errors: list[BaseException] = []
    thread = threading.Thread(target=lambda: _capture_error(connection.read, errors))
    thread.start()
    assert backend.read_entered.wait(timeout=1)

    connection.close()
    thread.join(timeout=1)

    assert thread.is_alive() is False
    assert isinstance(errors[0], PipeClosedError)


def test_connection_writes_exact_bytes() -> None:
    backend = FakeBackend()
    connection = NamedPipeClient("pipe", backend=backend).connect(timeout=0.5)

    connection.write(b"frame")

    assert backend.writes == [b"frame"]


def _capture_error(call: object, errors: list[BaseException]) -> None:
    try:
        call()  # type: ignore[operator]
    except BaseException as exc:
        errors.append(exc)
