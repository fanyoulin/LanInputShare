from __future__ import annotations

import os
import sys
import threading
import time
import uuid

import pytest

from lan_input_share.elevated.pipe_transport import (
    NamedPipeClient,
    NamedPipeServer,
    pipe_name_for,
)
from lan_input_share.windows.process_identity import current_user_sid

pytestmark = pytest.mark.windows_integration


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named pipes only")
def test_named_pipe_round_trip_and_reports_real_client_pid() -> None:
    sid = current_user_sid()
    name = pipe_name_for(sid, os.getpid()) + "." + uuid.uuid4().hex
    server = NamedPipeServer(name, sid)
    received: list[bytes] = []
    client_pids: list[int] = []

    def serve() -> None:
        connection = server.accept()
        try:
            client_pids.append(connection.client_pid)
            received.append(connection.read())
            connection.write(b"pong")
        finally:
            connection.close()

    thread = threading.Thread(target=serve)
    thread.start()
    client = None
    try:
        client = NamedPipeClient(name).connect(timeout=2.0)
        client.write(b"ping")
        assert client.read() == b"pong"
    finally:
        if client is not None:
            client.close()
        server.close()
        thread.join(timeout=2)

    assert thread.is_alive() is False
    assert received == [b"ping"]
    assert client_pids == [os.getpid()]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named pipes only")
def test_closing_server_cancels_real_blocked_accept() -> None:
    sid = current_user_sid()
    name = pipe_name_for(sid, os.getpid()) + "." + uuid.uuid4().hex
    server = NamedPipeServer(name, sid)
    thread = threading.Thread(target=_ignore_pipe_close, args=(server.accept,), daemon=True)
    thread.start()
    time.sleep(0.05)

    server.close()
    thread.join(timeout=1)

    assert thread.is_alive() is False


def _ignore_pipe_close(call: object) -> None:
    try:
        call()  # type: ignore[operator]
    except ConnectionError:
        return
