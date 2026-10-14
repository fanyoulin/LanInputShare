from __future__ import annotations

import asyncio
import base64
import inspect
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from lan_input_share.core.models import DeviceInfo, DeviceRole, ScreenInfo
from lan_input_share.protocol.codec import FrameDecoder, ProtocolError, encode_frame
from lan_input_share.protocol.messages import (
    PROTOCOL_VERSION,
    AuthChallengeMessage,
    AuthResponseMessage,
    HeartbeatMessage,
    HelloMessage,
    JsonValue,
    ProtocolVersionError,
    RoleConflictMessage,
    WireMessage,
)
from lan_input_share.security.identity import authentication_proof_payload, verify_signature


class SessionTransport(Protocol):
    async def read(self) -> bytes: ...

    async def write(self, data: bytes) -> None: ...

    async def close(self) -> None: ...

    def channel_binding(self) -> bytes | None: ...


type AuthenticationDecision = bool | CloseReason
type Authenticator = Callable[
    [DeviceInfo], AuthenticationDecision | Awaitable[AuthenticationDecision]
]
type CertificateAuthenticator = Callable[
    [DeviceInfo, bytes], bool | Awaitable[bool]
]
type CertificateApprover = Callable[[DeviceInfo, bytes], None | Awaitable[None]]
type ChallengeSigner = Callable[[bytes], bytes]


class SessionState(StrEnum):
    CONNECTING = "connecting"
    PAIRING = "pairing"
    AUTHENTICATED = "authenticated"
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"


class CloseReason(StrEnum):
    LOCAL_REQUEST = "local_request"
    REMOTE_CLOSED = "remote_closed"
    AUTHENTICATION_FAILED = "authentication_failed"
    ROLE_CONFLICT = "role_conflict"
    HEARTBEAT_TIMEOUT = "heartbeat_timeout"
    PROTOCOL_ERROR = "protocol_error"
    VERSION_MISMATCH = "version_mismatch"
    CONNECTION_ERROR = "connection_error"


@dataclass(frozen=True, slots=True)
class Connected:
    peer: DeviceInfo


@dataclass(frozen=True, slots=True)
class MessageReceived:
    message: WireMessage


@dataclass(frozen=True, slots=True)
class Disconnected:
    reason: CloseReason


type SessionEvent = Connected | MessageReceived | Disconnected


class ControlSession:
    """A framed control session that gates application data on peer authentication."""

    def __init__(
        self,
        transport: SessionTransport,
        local_device: DeviceInfo,
        authenticator: Authenticator,
        *,
        local_certificate_der: bytes | None = None,
        certificate_authenticator: CertificateAuthenticator | None = None,
        certificate_approver: CertificateApprover | None = None,
        challenge_signer: ChallengeSigner | None = None,
        heartbeat_interval: float = 2.0,
        heartbeat_timeout: float = 6.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if heartbeat_interval <= 0 or heartbeat_timeout <= heartbeat_interval:
            raise ValueError("心跳超时必须大于心跳间隔，且两者都必须为正数")
        secure_parts = (
            local_certificate_der,
            certificate_authenticator,
            challenge_signer,
        )
        if any(part is not None for part in secure_parts) and not all(
            part is not None for part in secure_parts
        ):
            raise ValueError("证书认证必须同时配置本机证书、信任校验和挑战签名器")
        if certificate_approver is not None and not all(
            part is not None for part in secure_parts
        ):
            raise ValueError("证书认证必须同时配置本机证书、信任校验和挑战签名器")
        self._transport = transport
        self._local_device = local_device
        self._authenticator = authenticator
        self._local_certificate_der = local_certificate_der
        self._certificate_authenticator = certificate_authenticator
        self._certificate_approver = certificate_approver
        self._challenge_signer = challenge_signer
        channel_binding_method = getattr(transport, "channel_binding", None)
        channel_binding = (
            channel_binding_method() if callable(channel_binding_method) else None
        )
        self._channel_binding = channel_binding if isinstance(channel_binding, bytes) else None
        if challenge_signer is not None and not self._channel_binding:
            raise ValueError("证书认证要求已建立 TLS 通道绑定")
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_timeout = heartbeat_timeout
        self._clock = clock
        self._decoder = FrameDecoder()
        self._events: asyncio.Queue[SessionEvent] = asyncio.Queue()
        self._state = SessionState.CONNECTING
        self._peer: DeviceInfo | None = None
        self._peer_session_id: str | None = None
        self._peer_certificate_der: bytes | None = None
        self._outstanding_challenge: bytes | None = None
        self._last_received = clock()
        self._receive_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._close_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._closed = asyncio.Event()
        self._started = False
        self._sequence = 0
        self.session_id = str(uuid.uuid4())

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def peer(self) -> DeviceInfo | None:
        return self._peer

    @property
    def peer_certificate_der(self) -> bytes | None:
        return self._peer_certificate_der

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._receive_task = asyncio.create_task(self._receive_loop())
        payload: dict[str, JsonValue] = {
            "protocol_version": PROTOCOL_VERSION,
            "device_id": self._local_device.device_id,
            "device_name": self._local_device.name,
            "role": self._local_device.role.value,
            "screen_width": self._local_device.screen.width,
            "screen_height": self._local_device.screen.height,
        }
        if self._local_certificate_der is not None:
            payload["certificate"] = base64.b64encode(
                self._local_certificate_der
            ).decode("ascii")
        await self._send_internal(
            HelloMessage(
                self.session_id,
                self._next_sequence(),
                payload,
            )
        )

    async def send(self, message: WireMessage) -> None:
        if self._state is not SessionState.ACTIVE:
            raise RuntimeError("会话尚未认证并激活")
        await self._send_internal(message)

    async def events(self) -> AsyncIterator[SessionEvent]:
        while True:
            event = await self._events.get()
            yield event
            if isinstance(event, Disconnected):
                return

    async def close(self, reason: CloseReason) -> None:
        async with self._close_lock:
            if self._state in (SessionState.CLOSING, SessionState.CLOSED):
                return
            self._state = SessionState.CLOSING
            current = asyncio.current_task()
            tasks = [
                task
                for task in (self._receive_task, self._heartbeat_task)
                if task is not None and task is not current and not task.done()
            ]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self._transport.close()
            self._state = SessionState.CLOSED
            self._closed.set()
            await self._events.put(Disconnected(reason))

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def _receive_loop(self) -> None:
        try:
            while self._state not in (SessionState.CLOSING, SessionState.CLOSED):
                data = await self._transport.read()
                if not data:
                    await self.close(CloseReason.REMOTE_CLOSED)
                    return
                self._last_received = self._clock()
                messages = self._decoder.feed(data)
                hello_seen = False
                for message in messages:
                    handshake_message = isinstance(
                        message,
                        (HelloMessage, AuthChallengeMessage, AuthResponseMessage),
                    )
                    if isinstance(message, HelloMessage):
                        hello_seen = True
                    if (
                        not handshake_message
                        and (self._state is not SessionState.ACTIVE or hello_seen)
                    ):
                        continue
                    await self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except ProtocolVersionError:
            await self.close(CloseReason.VERSION_MISMATCH)
        except ProtocolError:
            await self.close(CloseReason.PROTOCOL_ERROR)
        except (ConnectionError, OSError):
            await self.close(CloseReason.CONNECTION_ERROR)

    async def _handle_message(self, message: WireMessage) -> None:
        if isinstance(message, HelloMessage):
            await self._handle_hello(message)
            return
        if self._peer_session_id is not None and message.session_id != self._peer_session_id:
            await self.close(CloseReason.PROTOCOL_ERROR)
            return
        if isinstance(message, HeartbeatMessage):
            return
        if isinstance(message, AuthChallengeMessage):
            await self._handle_auth_challenge(message)
            return
        if isinstance(message, AuthResponseMessage):
            await self._handle_auth_response(message)
            return
        if isinstance(message, RoleConflictMessage):
            await self.close(CloseReason.ROLE_CONFLICT)
            return
        if self._state is SessionState.ACTIVE:
            await self._events.put(MessageReceived(message))

    async def _handle_hello(self, message: HelloMessage) -> None:
        if self._peer is not None:
            await self.close(CloseReason.PROTOCOL_ERROR)
            return
        if message.payload.get("protocol_version") != PROTOCOL_VERSION:
            await self.close(CloseReason.VERSION_MISMATCH)
            return
        try:
            peer = DeviceInfo(
                device_id=_required_string(message.payload.get("device_id")),
                name=_required_string(message.payload.get("device_name")),
                role=DeviceRole(_required_string(message.payload.get("role"))),
                screen=ScreenInfo(
                    _required_int(message.payload.get("screen_width")),
                    _required_int(message.payload.get("screen_height")),
                ),
            )
        except (ValueError, TypeError):
            await self.close(CloseReason.PROTOCOL_ERROR)
            return
        self._peer = peer
        self._peer_session_id = message.session_id
        raw_certificate = message.payload.get("certificate")
        if isinstance(raw_certificate, str):
            try:
                self._peer_certificate_der = base64.b64decode(
                    raw_certificate, validate=True
                )
            except ValueError:
                await self.close(CloseReason.PROTOCOL_ERROR)
                return
        if self._local_device.role is peer.role:
            await self._send_internal(
                RoleConflictMessage(
                    self.session_id,
                    self._next_sequence(),
                    {"reason": "controller_already_active"},
                )
            )
            await self.close(CloseReason.ROLE_CONFLICT)
            return

        self._state = SessionState.PAIRING
        try:
            result = self._authenticator(peer)
            decision = await result if inspect.isawaitable(result) else result
            if isinstance(decision, CloseReason):
                if decision is CloseReason.ROLE_CONFLICT:
                    await self._send_internal(
                        RoleConflictMessage(
                            self.session_id,
                            self._next_sequence(),
                            {"reason": "controller_already_active"},
                        )
                    )
                await self.close(decision)
                return
            authenticated = bool(decision)
            if authenticated and self._certificate_authenticator is not None:
                if self._peer_certificate_der is None:
                    authenticated = False
                else:
                    certificate_result = self._certificate_authenticator(
                        peer, self._peer_certificate_der
                    )
                    authenticated = (
                        bool(await certificate_result)
                        if inspect.isawaitable(certificate_result)
                        else bool(certificate_result)
                    )
        except Exception:
            authenticated = False
        if not authenticated:
            await self.close(CloseReason.AUTHENTICATION_FAILED)
            return
        self._state = SessionState.AUTHENTICATED
        self._last_received = self._clock()
        if self._challenge_signer is not None:
            self._outstanding_challenge = secrets.token_bytes(32)
            await self._send_internal(
                AuthChallengeMessage(
                    self.session_id,
                    self._next_sequence(),
                    {
                        "challenge": base64.b64encode(
                            self._outstanding_challenge
                        ).decode("ascii")
                    },
                )
            )
            return
        await self._activate()

    async def _handle_auth_challenge(self, message: AuthChallengeMessage) -> None:
        if self._state is not SessionState.AUTHENTICATED or self._challenge_signer is None:
            await self.close(CloseReason.PROTOCOL_ERROR)
            return
        raw_challenge = message.payload.get("challenge")
        if not isinstance(raw_challenge, str):
            await self.close(CloseReason.PROTOCOL_ERROR)
            return
        try:
            challenge = base64.b64decode(raw_challenge, validate=True)
        except ValueError:
            await self.close(CloseReason.PROTOCOL_ERROR)
            return
        if len(challenge) != 32:
            await self.close(CloseReason.PROTOCOL_ERROR)
            return
        assert self._channel_binding is not None
        signature = self._challenge_signer(
            authentication_proof_payload(self._channel_binding, challenge)
        )
        await self._send_internal(
            AuthResponseMessage(
                self.session_id,
                self._next_sequence(),
                {"signature": base64.b64encode(signature).decode("ascii")},
            )
        )

    async def _handle_auth_response(self, message: AuthResponseMessage) -> None:
        challenge = self._outstanding_challenge
        certificate = self._peer_certificate_der
        raw_signature = message.payload.get("signature")
        if (
            self._state is not SessionState.AUTHENTICATED
            or challenge is None
            or certificate is None
            or not isinstance(raw_signature, str)
        ):
            await self.close(CloseReason.PROTOCOL_ERROR)
            return
        try:
            signature = base64.b64decode(raw_signature, validate=True)
            assert self._channel_binding is not None
            verify_signature(
                certificate,
                authentication_proof_payload(self._channel_binding, challenge),
                signature,
            )
        except ValueError:
            await self.close(CloseReason.AUTHENTICATION_FAILED)
            return
        if self._certificate_approver is not None:
            try:
                assert self._peer is not None
                approval_result = self._certificate_approver(self._peer, certificate)
                if inspect.isawaitable(approval_result):
                    await approval_result
            except Exception:
                await self.close(CloseReason.AUTHENTICATION_FAILED)
                return
        self._outstanding_challenge = None
        await self._activate()

    async def _activate(self) -> None:
        if self._state is SessionState.ACTIVE:
            return
        self._state = SessionState.ACTIVE
        assert self._peer is not None
        await self._events.put(Connected(self._peer))
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        try:
            while self._state is SessionState.ACTIVE:
                await asyncio.sleep(self._heartbeat_interval)
                if self._clock() - self._last_received >= self._heartbeat_timeout:
                    await self.close(CloseReason.HEARTBEAT_TIMEOUT)
                    return
                await self._send_internal(
                    HeartbeatMessage(
                        self.session_id,
                        self._next_sequence(),
                        {"sent_at": self._clock()},
                    )
                )
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError):
            await self.close(CloseReason.CONNECTION_ERROR)

    async def _send_internal(self, message: WireMessage) -> None:
        async with self._send_lock:
            await self._transport.write(encode_frame(message))

    def _next_sequence(self) -> int:
        sequence = self._sequence
        self._sequence += 1
        return sequence


def _required_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("字段必须是非空字符串")
    return value


def _required_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("字段必须是整数")
    return value


__all__ = [
    "Authenticator",
    "CertificateApprover",
    "CertificateAuthenticator",
    "ChallengeSigner",
    "CloseReason",
    "Connected",
    "ControlSession",
    "Disconnected",
    "MessageReceived",
    "SessionEvent",
    "SessionState",
    "SessionTransport",
]
