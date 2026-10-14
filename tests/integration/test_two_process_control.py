from __future__ import annotations

import asyncio
import multiprocessing
import socket
from multiprocessing.connection import Connection

import pytest

from lan_input_share.config.paths import AppPaths
from lan_input_share.core.models import DeviceInfo, DeviceRole, Endpoint, ScreenInfo
from lan_input_share.input.events import KeyEvent
from lan_input_share.input.key_state import PressedInputState
from lan_input_share.network.connection import (
    SessionCallbacks,
    SessionListener,
    connect_session,
)
from lan_input_share.network.session import CloseReason, MessageReceived
from lan_input_share.protocol.messages import ClipboardMessage
from lan_input_share.runtime.controller import decode_input_event, encode_input_event
from lan_input_share.security.identity import DeviceIdentity, PeerHello
from lan_input_share.security.pairing import PairingCoordinator
from lan_input_share.security.tls import create_server_ssl_context
from lan_input_share.security.trust import TrustStore


def client_process(connection: Connection) -> None:
    state = PressedInputState()
    received: list[object] = []
    while True:
        payload = connection.recv()
        if payload == "disconnect":
            releases = state.release_events()
            connection.send((received, releases))
            return
        event = decode_input_event(payload)
        state.apply(event)
        received.append(event)


def test_two_process_ordered_delivery_and_disconnect_release() -> None:
    parent, child = multiprocessing.Pipe()
    process = multiprocessing.Process(target=client_process, args=(child,))
    process.start()
    try:
        first = KeyEvent(29, 0x11, True)
        second = KeyEvent(30, 65, True)
        parent.send(encode_input_event(first))
        parent.send(encode_input_event(second))
        parent.send("disconnect")
        received, releases = parent.recv()
    finally:
        process.join(timeout=3)
        if process.is_alive():
            process.terminate()

    assert received == [first, second]
    assert releases == (KeyEvent(29, 0x11, False), KeyEvent(30, 65, False))
    assert process.exitcode == 0


def _identity(root, name: str) -> DeviceIdentity:  # type: ignore[no-untyped-def]
    return DeviceIdentity.load_or_create(
        AppPaths(root, root / "config.json", root / "logs", root / "identity", root / "staging"),
        name,
    )


def _device(identity: DeviceIdentity, role: DeviceRole) -> DeviceInfo:
    return DeviceInfo(identity.device_id, identity.device_name, role, ScreenInfo(1920, 1080))


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.asyncio
async def test_client_listen_pair_clipboard_disconnect_and_reconnect(tmp_path) -> None:  # type: ignore[no-untyped-def]
    controller_identity = _identity(tmp_path / "controller", "controller")
    client_identity = _identity(tmp_path / "client", "client")
    client_trust = TrustStore(tmp_path / "client-trust.json")
    pairing = PairingCoordinator(client_identity, client_trust)
    approvals = 0
    accepted: asyncio.Queue[object] = asyncio.Queue()

    async def approve(_request):  # type: ignore[no-untyped-def]
        nonlocal approvals
        approvals += 1
        return True

    async def approve_certificate(peer: DeviceInfo, certificate: bytes) -> None:
        bound = client_trust.bound_controller()
        if bound is not None:
            assert client_trust.verify(peer.device_id, certificate)
            return
        assert await pairing.decide_controller(
            PeerHello(peer.device_id, peer.name, certificate), approve, timeout=1
        )

    def verify_controller(peer: DeviceInfo, certificate: bytes) -> bool:
        bound = client_trust.bound_controller()
        return bound is None or (
            bound.device_id == peer.device_id
            and client_trust.verify(peer.device_id, certificate)
        )

    endpoint = Endpoint("127.0.0.1", _unused_port())

    async def start_listener() -> SessionListener:
        listener = SessionListener(
            _device(client_identity, DeviceRole.CLIENT),
            lambda peer: peer.role is DeviceRole.CONTROLLER,
            ssl_context=create_server_ssl_context(client_identity),
            local_certificate_der=client_identity.certificate_der,
            certificate_authenticator=verify_controller,
            certificate_approver=approve_certificate,
            challenge_signer=client_identity.sign,
        )
        await listener.start(endpoint, accepted.put)
        return listener

    callbacks = SessionCallbacks(
        authenticator=lambda peer: peer.role is DeviceRole.CLIENT,
        certificate_authenticator=lambda _peer, certificate: (
            certificate == client_identity.certificate_der
        ),
    )
    listener = await start_listener()
    controller = await connect_session(
        endpoint,
        _device(controller_identity, DeviceRole.CONTROLLER),
        controller_identity,
        callbacks,
        "127.0.0.1",
    )
    client = await asyncio.wait_for(accepted.get(), timeout=2)
    message = ClipboardMessage(
        controller.session_id,
        10,
        {
            "origin_id": controller_identity.device_id,
            "sequence": 1,
            "digest": "test",
            "kind": "text",
            "payload": "你好，重连测试",
        },
    )
    await controller.send(message)

    assert await asyncio.wait_for(anext(client.events()), timeout=2) == MessageReceived(message)
    assert approvals == 1
    await controller.close(CloseReason.LOCAL_REQUEST)
    await listener.stop()

    listener = await start_listener()
    reconnected = await connect_session(
        endpoint,
        _device(controller_identity, DeviceRole.CONTROLLER),
        controller_identity,
        callbacks,
        "127.0.0.1",
    )
    _ = await asyncio.wait_for(accepted.get(), timeout=2)

    assert approvals == 1
    await reconnected.close(CloseReason.LOCAL_REQUEST)
    await listener.stop()
