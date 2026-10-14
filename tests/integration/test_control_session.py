from __future__ import annotations

import asyncio
import socket

import pytest

from lan_input_share.config.paths import AppPaths
from lan_input_share.core.models import DeviceInfo, DeviceRole, Endpoint, ScreenInfo
from lan_input_share.network.connection import (
    ControllerServer,
    SessionCallbacks,
    SessionListener,
    StreamTransport,
    connect_session,
)
from lan_input_share.network.session import CloseReason, Connected, SessionState
from lan_input_share.protocol.codec import encode_frame
from lan_input_share.protocol.messages import PROTOCOL_VERSION, HelloMessage
from lan_input_share.security.identity import DeviceIdentity
from lan_input_share.security.tls import create_server_ssl_context


def device(device_id: str, role: DeviceRole) -> DeviceInfo:
    return DeviceInfo(device_id, device_id, role, ScreenInfo(1920, 1080))


async def allow(_peer: DeviceInfo) -> bool:
    return True


async def open_client(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection("127.0.0.1", port)


def unused_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.asyncio
async def test_protocol_error_closes_only_the_bad_session() -> None:
    server = ControllerServer(
        device("controller", DeviceRole.CONTROLLER),
        allow,
        heartbeat_interval=60,
        heartbeat_timeout=120,
    )
    endpoint = await server.start(Endpoint("127.0.0.1", unused_loopback_port()))
    bad_reader, bad_writer = await open_client(endpoint.port)
    good_reader, good_writer = await open_client(endpoint.port)
    good = StreamTransport(good_reader, good_writer)

    bad_writer.write(b"\x00\x00\x00\x00")
    await bad_writer.drain()
    await good.write(
        encode_frame(
            HelloMessage(
                "good-session",
                0,
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "device_id": "client",
                    "device_name": "client",
                    "role": "client",
                    "screen_width": 1920,
                    "screen_height": 1080,
                },
            )
        )
    )

    await asyncio.wait_for(server.wait_for_session_count(2), timeout=1)
    await asyncio.sleep(0.03)
    sessions = server.sessions()
    assert sum(session.state is SessionState.ACTIVE for session in sessions) == 1
    active = next(session for session in sessions if session.state is SessionState.ACTIVE)
    assert isinstance(await asyncio.wait_for(anext(active.events()), timeout=1), Connected)

    assert await asyncio.wait_for(bad_reader.read(), timeout=1)
    assert await asyncio.wait_for(bad_reader.read(), timeout=1) == b""
    await good.close()
    await asyncio.wait_for(server.stop(), timeout=2)


@pytest.mark.asyncio
async def test_server_stop_closes_all_sessions() -> None:
    server = ControllerServer(device("controller", DeviceRole.CONTROLLER), allow)
    endpoint = await server.start(Endpoint("127.0.0.1", unused_loopback_port()))
    reader, writer = await open_client(endpoint.port)
    _ = reader
    await asyncio.wait_for(server.wait_for_session_count(1), timeout=1)

    await asyncio.wait_for(server.stop(), timeout=2)

    assert all(session.state is SessionState.CLOSED for session in server.sessions())
    writer.close()
    await asyncio.wait_for(writer.wait_closed(), timeout=2)


def identity(root, name: str) -> DeviceIdentity:  # type: ignore[no-untyped-def]
    return DeviceIdentity.load_or_create(
        AppPaths(root, root / "config.json", root / "logs", root / "identity", root / "staging"),
        name,
    )


@pytest.mark.asyncio
async def test_client_listener_accepts_controller_outbound_session(tmp_path) -> None:  # type: ignore[no-untyped-def]
    controller_identity = identity(tmp_path / "controller", "controller")
    client_identity = identity(tmp_path / "client", "client")
    accepted: asyncio.Future[object] = asyncio.Future()
    listener = SessionListener(
        device("client", DeviceRole.CLIENT),
        allow,
        ssl_context=create_server_ssl_context(client_identity),
        local_certificate_der=client_identity.certificate_der,
        certificate_authenticator=lambda peer, certificate: (
            peer.role is DeviceRole.CONTROLLER
            and certificate == controller_identity.certificate_der
        ),
        challenge_signer=client_identity.sign,
    )

    endpoint = await listener.start(
        Endpoint("127.0.0.1", unused_loopback_port()),
        lambda session: accepted.set_result(session),
    )
    controller_session = await connect_session(
        endpoint,
        device("controller", DeviceRole.CONTROLLER),
        controller_identity,
        SessionCallbacks(
            authenticator=allow,
            certificate_authenticator=lambda peer, certificate: (
                peer.role is DeviceRole.CLIENT
                and certificate == client_identity.certificate_der
            ),
        ),
        local_ip="127.0.0.1",
    )
    client_session = await asyncio.wait_for(accepted, timeout=2)

    assert controller_session.state is SessionState.ACTIVE
    assert controller_session.peer is not None
    assert controller_session.peer.role is DeviceRole.CLIENT
    assert client_session.state is SessionState.ACTIVE
    assert client_session.peer.role is DeviceRole.CONTROLLER

    await controller_session.close(CloseReason.LOCAL_REQUEST)
    await listener.stop()
