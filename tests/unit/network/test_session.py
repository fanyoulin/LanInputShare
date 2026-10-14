from __future__ import annotations

import asyncio
import base64
import json
import struct
from collections.abc import AsyncIterator

import pytest

from lan_input_share.config.paths import AppPaths
from lan_input_share.core.models import DeviceInfo, DeviceRole, ScreenInfo
from lan_input_share.network.session import (
    CloseReason,
    Connected,
    ControlSession,
    Disconnected,
    MessageReceived,
    SessionState,
)
from lan_input_share.protocol.codec import FrameDecoder, encode_frame
from lan_input_share.protocol.messages import (
    PROTOCOL_VERSION,
    AuthChallengeMessage,
    AuthResponseMessage,
    HelloMessage,
    InputMessage,
)
from lan_input_share.security.identity import (
    DeviceIdentity,
    authentication_proof_payload,
)


class QueueTransport:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.peer: QueueTransport | None = None
        self.close_calls = 0
        self.channel_binding_data = b"unit-test-tls-channel"

    async def read(self) -> bytes:
        data = await self.incoming.get()
        return b"" if data is None else data

    async def write(self, data: bytes) -> None:
        assert self.peer is not None
        await self.peer.incoming.put(data)

    async def close(self) -> None:
        self.close_calls += 1
        if self.peer is not None:
            await self.peer.incoming.put(None)

    def channel_binding(self) -> bytes:
        return self.channel_binding_data


def transport_pair() -> tuple[QueueTransport, QueueTransport]:
    left = QueueTransport()
    right = QueueTransport()
    left.peer = right
    right.peer = left
    return left, right


def device(device_id: str, role: DeviceRole) -> DeviceInfo:
    return DeviceInfo(device_id, device_id, role, ScreenInfo(1920, 1080))


async def allow(_peer: DeviceInfo) -> bool:
    return True


async def next_event(session: ControlSession) -> object:
    iterator: AsyncIterator[object] = session.events()
    return await asyncio.wait_for(anext(iterator), timeout=1)


def identity(root, name: str) -> DeviceIdentity:  # type: ignore[no-untyped-def]
    paths = AppPaths(
        root,
        root / "config.json",
        root / "logs",
        root / "identity",
        root / "staging",
    )
    return DeviceIdentity.load_or_create(paths, name)


def test_certificate_approver_requires_secure_authentication_configuration() -> None:
    local, _remote = transport_pair()

    with pytest.raises(ValueError, match="证书认证必须同时配置"):
        ControlSession(
            local,
            device("client", DeviceRole.CLIENT),
            allow,
            certificate_approver=lambda _peer, _certificate: None,
        )


@pytest.mark.asyncio
async def test_no_application_message_is_emitted_before_authentication() -> None:
    local, remote = transport_pair()
    release_authentication = asyncio.Event()

    async def delayed_authentication(_peer: DeviceInfo) -> bool:
        await release_authentication.wait()
        return True

    session = ControlSession(
        local,
        device("client", DeviceRole.CLIENT),
        delayed_authentication,
        heartbeat_interval=60,
        heartbeat_timeout=120,
    )
    await session.start()
    await remote.write(
        encode_frame(
            HelloMessage(
                "remote-session",
                0,
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "device_id": "controller",
                    "device_name": "controller",
                    "role": "controller",
                    "screen_width": 1920,
                    "screen_height": 1080,
                },
            )
        )
        + encode_frame(InputMessage("remote-session", 1, {"kind": "move"}))
    )
    await asyncio.sleep(0)

    assert session.state is SessionState.PAIRING
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(session.events()), timeout=0.02)

    release_authentication.set()
    assert isinstance(await next_event(session), Connected)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(session.events()), timeout=0.02)
    await session.close(CloseReason.LOCAL_REQUEST)


@pytest.mark.asyncio
async def test_heartbeat_timeout_closes_session_once() -> None:
    local, remote = transport_pair()
    session = ControlSession(
        local,
        device("client", DeviceRole.CLIENT),
        allow,
        heartbeat_interval=0.01,
        heartbeat_timeout=0.03,
    )
    await session.start()
    await remote.write(
        encode_frame(
            HelloMessage(
                "remote-session",
                0,
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "device_id": "controller",
                    "device_name": "controller",
                    "role": "controller",
                    "screen_width": 1920,
                    "screen_height": 1080,
                },
            )
        )
    )

    assert isinstance(await next_event(session), Connected)
    disconnected = await next_event(session)
    assert disconnected == Disconnected(CloseReason.HEARTBEAT_TIMEOUT)
    await asyncio.sleep(0.03)
    assert local.close_calls == 1
    assert session.state is SessionState.CLOSED


@pytest.mark.asyncio
async def test_second_controller_receives_role_conflict_and_cannot_activate() -> None:
    local, remote = transport_pair()
    session = ControlSession(
        local,
        device("controller-a", DeviceRole.CONTROLLER),
        allow,
        heartbeat_interval=60,
        heartbeat_timeout=120,
    )
    await session.start()
    await remote.write(
        encode_frame(
            HelloMessage(
                "controller-b-session",
                0,
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "device_id": "controller-b",
                    "device_name": "controller-b",
                    "role": "controller",
                    "screen_width": 2560,
                    "screen_height": 1440,
                },
            )
        )
    )

    event = await next_event(session)
    assert event == Disconnected(CloseReason.ROLE_CONFLICT)
    assert session.state is SessionState.CLOSED
    decoder = FrameDecoder()
    sent = decoder.feed(await remote.read()) + decoder.feed(await remote.read())
    assert [message.TYPE.value for message in sent] == ["hello", "role_conflict"]


@pytest.mark.asyncio
async def test_authenticator_can_report_client_busy_as_role_conflict() -> None:
    local, remote = transport_pair()
    session = ControlSession(
        local,
        device("client", DeviceRole.CLIENT),
        lambda _peer: CloseReason.ROLE_CONFLICT,
        heartbeat_interval=60,
        heartbeat_timeout=120,
    )
    await session.start()
    await remote.write(
        encode_frame(
            HelloMessage(
                "controller-session",
                0,
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "device_id": "controller",
                    "device_name": "controller",
                    "role": "controller",
                    "screen_width": 1920,
                    "screen_height": 1080,
                },
            )
        )
    )

    assert await next_event(session) == Disconnected(CloseReason.ROLE_CONFLICT)
    assert session.state is SessionState.CLOSED


@pytest.mark.asyncio
async def test_two_clients_receive_role_conflict_and_cannot_activate() -> None:
    left_transport, right_transport = transport_pair()
    first = ControlSession(
        left_transport,
        device("client-a", DeviceRole.CLIENT),
        allow,
        heartbeat_interval=60,
        heartbeat_timeout=120,
    )
    second = ControlSession(
        right_transport,
        device("client-b", DeviceRole.CLIENT),
        allow,
        heartbeat_interval=60,
        heartbeat_timeout=120,
    )

    await asyncio.gather(first.start(), second.start())

    assert await next_event(first) == Disconnected(CloseReason.ROLE_CONFLICT)
    assert await next_event(second) == Disconnected(CloseReason.ROLE_CONFLICT)
    assert first.state is SessionState.CLOSED
    assert second.state is SessionState.CLOSED


@pytest.mark.asyncio
async def test_handshake_exchanges_protocol_role_device_and_screen_information() -> None:
    left_transport, right_transport = transport_pair()
    controller = ControlSession(
        left_transport,
        device("desk", DeviceRole.CONTROLLER),
        allow,
        heartbeat_interval=60,
        heartbeat_timeout=120,
    )
    client = ControlSession(
        right_transport,
        DeviceInfo("laptop", "Laptop", DeviceRole.CLIENT, ScreenInfo(1366, 768)),
        allow,
        heartbeat_interval=60,
        heartbeat_timeout=120,
    )

    await asyncio.gather(controller.start(), client.start())
    controller_event, client_event = await asyncio.gather(
        next_event(controller), next_event(client)
    )

    assert controller_event == Connected(
        DeviceInfo("laptop", "Laptop", DeviceRole.CLIENT, ScreenInfo(1366, 768))
    )
    assert client_event == Connected(device("desk", DeviceRole.CONTROLLER))
    assert controller.state is SessionState.ACTIVE
    assert client.state is SessionState.ACTIVE
    await asyncio.gather(
        controller.close(CloseReason.LOCAL_REQUEST),
        client.close(CloseReason.LOCAL_REQUEST),
    )


def test_version_mismatch_has_distinct_displayable_close_reason() -> None:
    assert getattr(CloseReason, "VERSION_MISMATCH", None) is not None


@pytest.mark.asyncio
async def test_version_one_peer_is_rejected_by_version_two_session() -> None:
    local, remote = transport_pair()
    session = ControlSession(
        local,
        device("client", DeviceRole.CLIENT),
        allow,
        heartbeat_interval=60,
        heartbeat_timeout=120,
    )
    await session.start()
    body = json.dumps(
        {
            "version": 1,
            "type": "hello",
            "session_id": "old-session",
            "sequence": 0,
            "payload": {
                "protocol_version": 1,
                "device_id": "old-controller",
                "device_name": "old-controller",
                "role": "controller",
                "screen_width": 1920,
                "screen_height": 1080,
            },
        }
    ).encode("utf-8")
    await remote.write(struct.pack(">I", len(body)) + body)

    mismatch_reason = getattr(
        CloseReason,
        "VERSION_MISMATCH",
        CloseReason.PROTOCOL_ERROR,
    )
    assert await next_event(session) == Disconnected(mismatch_reason)
    assert session.state is SessionState.CLOSED


@pytest.mark.asyncio
async def test_active_application_message_is_emitted() -> None:
    left_transport, right_transport = transport_pair()
    controller = ControlSession(
        left_transport,
        device("desk", DeviceRole.CONTROLLER),
        allow,
        heartbeat_interval=60,
        heartbeat_timeout=120,
    )
    client = ControlSession(
        right_transport,
        device("laptop", DeviceRole.CLIENT),
        allow,
        heartbeat_interval=60,
        heartbeat_timeout=120,
    )
    await asyncio.gather(controller.start(), client.start())
    await asyncio.gather(next_event(controller), next_event(client))

    message = InputMessage(controller.session_id, 10, {"kind": "move"})
    await controller.send(message)

    assert await next_event(client) == MessageReceived(message)
    await asyncio.gather(
        controller.close(CloseReason.LOCAL_REQUEST),
        client.close(CloseReason.LOCAL_REQUEST),
    )


@pytest.mark.asyncio
async def test_certificate_sessions_prove_private_key_possession(tmp_path) -> None:  # type: ignore[no-untyped-def]
    controller_identity = identity(tmp_path / "controller", "controller")
    client_identity = identity(tmp_path / "client", "client")
    approved: list[tuple[str, bytes]] = []
    left_transport, right_transport = transport_pair()
    controller = ControlSession(
        left_transport,
        device("desk", DeviceRole.CONTROLLER),
        allow,
        local_certificate_der=controller_identity.certificate_der,
        certificate_authenticator=lambda _peer, cert: cert
        == client_identity.certificate_der,
        certificate_approver=lambda peer, cert: approved.append(
            (peer.device_id, cert)
        ),
        challenge_signer=controller_identity.sign,
        heartbeat_interval=60,
        heartbeat_timeout=120,
    )
    client = ControlSession(
        right_transport,
        device("laptop", DeviceRole.CLIENT),
        allow,
        local_certificate_der=client_identity.certificate_der,
        certificate_authenticator=lambda _peer, cert: cert
        == controller_identity.certificate_der,
        challenge_signer=client_identity.sign,
        heartbeat_interval=60,
        heartbeat_timeout=120,
    )

    await asyncio.gather(controller.start(), client.start())

    assert isinstance(await next_event(controller), Connected)
    assert isinstance(await next_event(client), Connected)
    assert controller.state is SessionState.ACTIVE
    assert client.state is SessionState.ACTIVE
    assert approved == [("laptop", client_identity.certificate_der)]
    await asyncio.gather(
        controller.close(CloseReason.LOCAL_REQUEST),
        client.close(CloseReason.LOCAL_REQUEST),
    )


@pytest.mark.asyncio
async def test_copied_certificate_without_private_key_is_rejected(tmp_path) -> None:  # type: ignore[no-untyped-def]
    controller_identity = identity(tmp_path / "controller", "controller")
    expected_client = identity(tmp_path / "expected", "expected")
    attacker = identity(tmp_path / "attacker", "attacker")
    approved: list[tuple[str, bytes]] = []
    left_transport, right_transport = transport_pair()
    controller = ControlSession(
        left_transport,
        device("desk", DeviceRole.CONTROLLER),
        allow,
        local_certificate_der=controller_identity.certificate_der,
        certificate_authenticator=lambda _peer, cert: cert
        == expected_client.certificate_der,
        certificate_approver=lambda peer, cert: approved.append(
            (peer.device_id, cert)
        ),
        challenge_signer=controller_identity.sign,
        heartbeat_interval=60,
        heartbeat_timeout=120,
    )
    spoofed_client = ControlSession(
        right_transport,
        device("laptop", DeviceRole.CLIENT),
        allow,
        local_certificate_der=expected_client.certificate_der,
        certificate_authenticator=lambda _peer, cert: cert
        == controller_identity.certificate_der,
        challenge_signer=attacker.sign,
        heartbeat_interval=60,
        heartbeat_timeout=120,
    )

    await asyncio.gather(controller.start(), spoofed_client.start())

    assert await next_event(controller) == Disconnected(
        CloseReason.AUTHENTICATION_FAILED
    )
    assert controller.state is SessionState.CLOSED
    assert spoofed_client.state is not SessionState.ACTIVE
    assert approved == []
    await spoofed_client.close(CloseReason.LOCAL_REQUEST)


@pytest.mark.asyncio
async def test_relayed_private_key_proof_from_different_tls_channel_is_rejected(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    controller_identity = identity(tmp_path / "controller", "controller")
    client_identity = identity(tmp_path / "client", "client")
    left_transport, right_transport = transport_pair()
    left_transport.channel_binding_data = b"mitm-controller-leg"
    right_transport.channel_binding_data = b"mitm-client-leg"
    controller = ControlSession(
        left_transport,
        device("desk", DeviceRole.CONTROLLER),
        allow,
        local_certificate_der=controller_identity.certificate_der,
        certificate_authenticator=lambda _peer, cert: cert
        == client_identity.certificate_der,
        challenge_signer=controller_identity.sign,
        heartbeat_interval=60,
        heartbeat_timeout=120,
    )
    client = ControlSession(
        right_transport,
        device("laptop", DeviceRole.CLIENT),
        allow,
        local_certificate_der=client_identity.certificate_der,
        certificate_authenticator=lambda _peer, cert: cert
        == controller_identity.certificate_der,
        challenge_signer=client_identity.sign,
        heartbeat_interval=60,
        heartbeat_timeout=120,
    )

    await asyncio.gather(controller.start(), client.start())
    await asyncio.sleep(0.05)

    assert controller.state is not SessionState.ACTIVE
    assert client.state is not SessionState.ACTIVE
    await asyncio.gather(
        controller.close(CloseReason.LOCAL_REQUEST),
        client.close(CloseReason.LOCAL_REQUEST),
    )


@pytest.mark.asyncio
async def test_auth_response_and_first_application_frame_can_share_one_read(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    local_identity = identity(tmp_path / "local", "local")
    remote_identity = identity(tmp_path / "remote", "remote")
    local, remote = transport_pair()
    session = ControlSession(
        local,
        device("desk", DeviceRole.CONTROLLER),
        allow,
        local_certificate_der=local_identity.certificate_der,
        certificate_authenticator=lambda _peer, cert: cert
        == remote_identity.certificate_der,
        challenge_signer=local_identity.sign,
        heartbeat_interval=60,
        heartbeat_timeout=120,
    )
    await session.start()
    _ = await remote.read()
    await remote.write(
        encode_frame(
            HelloMessage(
                "remote-session",
                0,
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "device_id": "laptop",
                    "device_name": "laptop",
                    "role": "client",
                    "screen_width": 1920,
                    "screen_height": 1080,
                    "certificate": base64.b64encode(
                        remote_identity.certificate_der
                    ).decode("ascii"),
                },
            )
        )
    )
    decoder = FrameDecoder()
    challenge_message = decoder.feed(await remote.read())[0]
    assert isinstance(challenge_message, AuthChallengeMessage)
    challenge = base64.b64decode(str(challenge_message.payload["challenge"]))
    first_input = InputMessage(
        "remote-session",
        2,
        {
            "kind": "key",
            "scan_code": 30,
            "virtual_key": 65,
            "is_down": True,
            "is_extended": False,
            "injected": False,
            "timestamp": 0.0,
        },
    )
    await remote.write(
        encode_frame(
            AuthResponseMessage(
                "remote-session",
                1,
                {
                    "signature": base64.b64encode(
                        remote_identity.sign(
                            authentication_proof_payload(
                                local.channel_binding_data, challenge
                            )
                        )
                    ).decode("ascii")
                },
            )
        )
        + encode_frame(first_input)
    )

    assert isinstance(await next_event(session), Connected)
    assert await next_event(session) == MessageReceived(first_input)
    await session.close(CloseReason.LOCAL_REQUEST)
