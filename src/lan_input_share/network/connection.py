from __future__ import annotations

import asyncio
import inspect
import socket
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum

from lan_input_share.core.models import DeviceInfo, Endpoint
from lan_input_share.network.backoff import ExponentialBackoff
from lan_input_share.network.session import (
    Authenticator,
    CertificateApprover,
    CertificateAuthenticator,
    ChallengeSigner,
    CloseReason,
    Connected,
    ControlSession,
    Disconnected,
)
from lan_input_share.security.identity import DeviceIdentity
from lan_input_share.security.tls import create_client_ssl_context

type SessionCallback = Callable[[ControlSession], None | Awaitable[None]]


@dataclass(frozen=True, slots=True)
class SessionCallbacks:
    authenticator: Authenticator
    certificate_authenticator: CertificateAuthenticator
    certificate_approver: CertificateApprover | None = None


class SessionConnectError(ConnectionError):
    def __init__(self, reason: CloseReason) -> None:
        super().__init__(f"会话连接失败：{reason.value}")
        self.reason = reason


class StreamTransport:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._closed = False

    async def read(self) -> bytes:
        return await self._reader.read(65_536)

    async def write(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionError("连接已经关闭")
        self._writer.write(data)
        await self._writer.drain()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._writer.close()
        with suppress(TimeoutError, ConnectionError, OSError):
            await asyncio.wait_for(self._writer.wait_closed(), timeout=0.5)

    def channel_binding(self) -> bytes | None:
        ssl_object = self._writer.get_extra_info("ssl_object")
        getter = getattr(ssl_object, "get_channel_binding", None)
        if not callable(getter):
            return None
        value = getter("tls-unique")
        return bytes(value) if value else None


class SessionListener:
    def __init__(
        self,
        local_device: DeviceInfo,
        authenticator: Authenticator,
        *,
        ssl_context: ssl.SSLContext | None = None,
        local_certificate_der: bytes | None = None,
        certificate_authenticator: CertificateAuthenticator | None = None,
        certificate_approver: CertificateApprover | None = None,
        challenge_signer: ChallengeSigner | None = None,
        heartbeat_interval: float = 2.0,
        heartbeat_timeout: float = 6.0,
        handshake_timeout: float = 10.0,
    ) -> None:
        self._local_device = local_device
        self._authenticator = authenticator
        self._ssl_context = ssl_context
        self._local_certificate_der = local_certificate_der
        self._certificate_authenticator = certificate_authenticator
        self._certificate_approver = certificate_approver
        self._challenge_signer = challenge_signer
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_timeout = heartbeat_timeout
        self._handshake_timeout = handshake_timeout
        self._server: asyncio.Server | None = None
        self._sessions: list[ControlSession] = []
        self._on_session: SessionCallback | None = None

    async def start(
        self, endpoint: Endpoint, on_session: SessionCallback | None = None
    ) -> Endpoint:
        if self._server is not None:
            raise RuntimeError("会话监听器已经启动")
        self._on_session = on_session
        self._server = await asyncio.start_server(
            self._accept,
            endpoint.host,
            endpoint.port,
            ssl=self._ssl_context,
        )
        socket = self._server.sockets[0]
        host, port = socket.getsockname()[:2]
        return Endpoint(str(host), int(port))

    def sessions(self) -> tuple[ControlSession, ...]:
        return tuple(self._sessions)

    async def wait_for_session_count(self, count: int) -> None:
        while len(self._sessions) < count:
            await asyncio.sleep(0)

    async def stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
        await asyncio.gather(
            *(session.close(CloseReason.LOCAL_REQUEST) for session in self._sessions),
            return_exceptions=True,
        )
        if server is not None:
            await server.wait_closed()

    async def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        _enable_low_latency(writer)
        session = ControlSession(
            StreamTransport(reader, writer),
            self._local_device,
            self._authenticator,
            local_certificate_der=self._local_certificate_der,
            certificate_authenticator=self._certificate_authenticator,
            certificate_approver=self._certificate_approver,
            challenge_signer=self._challenge_signer,
            heartbeat_interval=self._heartbeat_interval,
            heartbeat_timeout=self._heartbeat_timeout,
        )
        self._sessions.append(session)
        try:
            await session.start()
            if self._on_session is not None:
                first = await asyncio.wait_for(
                    anext(session.events()), timeout=self._handshake_timeout
                )
                if not isinstance(first, Connected):
                    return
                result = self._on_session(session)
                if inspect.isawaitable(result):
                    await result
        except (ConnectionError, OSError, TimeoutError, StopAsyncIteration):
            await session.close(CloseReason.CONNECTION_ERROR)
        except Exception:
            await session.close(CloseReason.AUTHENTICATION_FAILED)


ControllerServer = SessionListener


async def connect_session(
    endpoint: Endpoint,
    local_device: DeviceInfo,
    identity: DeviceIdentity,
    callbacks: SessionCallbacks,
    local_ip: str | None = None,
    *,
    heartbeat_interval: float = 2.0,
    heartbeat_timeout: float = 6.0,
    handshake_timeout: float = 10.0,
) -> ControlSession:
    reader, writer = await asyncio.open_connection(
        endpoint.host,
        endpoint.port,
        ssl=create_client_ssl_context(),
        local_addr=(local_ip, 0) if local_ip else None,
    )
    _enable_low_latency(writer)
    session = ControlSession(
        StreamTransport(reader, writer),
        local_device,
        callbacks.authenticator,
        local_certificate_der=identity.certificate_der,
        certificate_authenticator=callbacks.certificate_authenticator,
        certificate_approver=callbacks.certificate_approver,
        challenge_signer=identity.sign,
        heartbeat_interval=heartbeat_interval,
        heartbeat_timeout=heartbeat_timeout,
    )
    try:
        await session.start()
        first = await asyncio.wait_for(anext(session.events()), timeout=handshake_timeout)
        if isinstance(first, Disconnected):
            raise SessionConnectError(first.reason)
        if not isinstance(first, Connected):
            raise ConnectionError("会话认证失败")
        return session
    except Exception:
        await session.close(CloseReason.CONNECTION_ERROR)
        raise


def _enable_low_latency(writer: asyncio.StreamWriter) -> None:
    raw_socket = writer.get_extra_info("socket")
    if raw_socket is not None:
        raw_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


class ConnectionStatus(StrEnum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    WAITING_RETRY = "waiting_retry"


@dataclass(frozen=True, slots=True)
class ConnectionState:
    status: ConnectionStatus
    attempt: int
    delay: float = 0.0
    reason: CloseReason | None = None
    session: ControlSession | None = None


class ClientConnector:
    def __init__(
        self,
        local_device: DeviceInfo,
        authenticator: Authenticator,
        *,
        ssl_context: ssl.SSLContext | None = None,
        backoff: ExponentialBackoff | None = None,
        heartbeat_interval: float = 2.0,
        heartbeat_timeout: float = 6.0,
    ) -> None:
        self._local_device = local_device
        self._authenticator = authenticator
        self._ssl_context = ssl_context
        self._backoff = backoff or ExponentialBackoff()
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_timeout = heartbeat_timeout
        self._reconnect = asyncio.Event()
        self._stop = asyncio.Event()
        self._active: ControlSession | None = None

    def request_reconnect(self) -> None:
        self._reconnect.set()

    async def stop(self) -> None:
        self._stop.set()
        self._reconnect.set()
        if self._active is not None:
            await self._active.close(CloseReason.LOCAL_REQUEST)

    async def run(self, controller: Endpoint) -> AsyncIterator[ConnectionState]:
        attempt = 0
        while not self._stop.is_set():
            attempt += 1
            yield ConnectionState(ConnectionStatus.CONNECTING, attempt)
            reason = CloseReason.CONNECTION_ERROR
            try:
                reader, writer = await asyncio.open_connection(
                    controller.host,
                    controller.port,
                    ssl=self._ssl_context,
                )
                session = ControlSession(
                    StreamTransport(reader, writer),
                    self._local_device,
                    self._authenticator,
                    heartbeat_interval=self._heartbeat_interval,
                    heartbeat_timeout=self._heartbeat_timeout,
                )
                self._active = session
                await session.start()
                first = await anext(session.events())
                if isinstance(first, Connected):
                    self._backoff.reset()
                    attempt = 0
                    yield ConnectionState(ConnectionStatus.CONNECTED, attempt, session=session)
                    terminal = await anext(session.events())
                    while not isinstance(terminal, Disconnected):
                        terminal = await anext(session.events())
                    reason = terminal.reason
                elif isinstance(first, Disconnected):
                    reason = first.reason
            except (ConnectionError, OSError, StopAsyncIteration):
                reason = CloseReason.CONNECTION_ERROR
            finally:
                self._active = None
            yield ConnectionState(ConnectionStatus.DISCONNECTED, attempt, reason=reason)
            if self._stop.is_set():
                return
            delay = self._backoff.next_delay()
            yield ConnectionState(ConnectionStatus.WAITING_RETRY, attempt, delay, reason)
            self._reconnect.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._reconnect.wait(), timeout=delay)


__all__ = [
    "ClientConnector",
    "ConnectionState",
    "ConnectionStatus",
    "ControllerServer",
    "SessionCallbacks",
    "SessionConnectError",
    "SessionListener",
    "StreamTransport",
    "connect_session",
]
