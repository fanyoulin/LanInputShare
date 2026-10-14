from __future__ import annotations

import asyncio
import json
import socket
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass

from lan_input_share.core.models import Endpoint
from lan_input_share.network.adapters import AdapterInfo, bind_socket


@dataclass(frozen=True, slots=True)
class DiscoveryAnnouncement:
    device_id: str
    device_name: str
    role: str
    port: int
    fingerprint: str
    version: int = 1

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> DiscoveryAnnouncement | None:
        if len(data) > 4096:
            return None
        try:
            payload = json.loads(data.decode("utf-8"))
            announcement = cls(**payload)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            return None
        if announcement.version != 1 or announcement.role not in {"controller", "client"}:
            return None
        if not announcement.device_id or not announcement.device_name:
            return None
        if not 1 <= announcement.port <= 65535:
            return None
        return announcement


@dataclass(frozen=True, slots=True)
class DiscoveredPeer:
    device_id: str
    device_name: str
    role: str
    port: int
    fingerprint: str
    addresses: tuple[str, ...]
    adapters: tuple[str, ...]


class DiscoveryRegistry:
    def __init__(self) -> None:
        self._items: dict[str, tuple[DiscoveryAnnouncement, set[str], set[str]]] = {}

    def update(self, announcement: DiscoveryAnnouncement, address: str, adapter_id: str) -> None:
        current = self._items.get(announcement.device_id)
        addresses = set() if current is None else current[1]
        adapters = set() if current is None else current[2]
        addresses.add(address)
        adapters.add(adapter_id)
        self._items[announcement.device_id] = (announcement, addresses, adapters)

    def peers(self) -> tuple[DiscoveredPeer, ...]:
        peers = []
        for announcement, addresses, adapters in self._items.values():
            peers.append(
                DiscoveredPeer(
                    announcement.device_id,
                    announcement.device_name,
                    announcement.role,
                    announcement.port,
                    announcement.fingerprint,
                    tuple(sorted(addresses)),
                    tuple(sorted(adapters)),
                )
            )
        return tuple(sorted(peers, key=lambda peer: peer.device_id))


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, service: DiscoveryService, adapter_id: str) -> None:
        self._service = service
        self._adapter_id = adapter_id

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        announcement = DiscoveryAnnouncement.from_bytes(data)
        if announcement is not None and announcement.device_id != self._service.local.device_id:
            self._service._registry.update(announcement, addr[0], self._adapter_id)
            self._service._queue.put_nowait(self._service._registry.peers())


class DiscoveryService:
    def __init__(
        self,
        local: DiscoveryAnnouncement,
        adapters: tuple[AdapterInfo, ...],
    ) -> None:
        self.local = local
        self._adapters = adapters
        self._registry = DiscoveryRegistry()
        self._queue: asyncio.Queue[tuple[DiscoveredPeer, ...]] = asyncio.Queue()
        self._transports: list[asyncio.DatagramTransport] = []

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        for adapter in self._adapters:
            def protocol_factory(adapter_id: str = adapter.adapter_id) -> asyncio.DatagramProtocol:
                return _DiscoveryProtocol(self, adapter_id)

            transport, _ = await loop.create_datagram_endpoint(
                protocol_factory,
                local_addr=(adapter.ipv4, 0),
                allow_broadcast=True,
            )
            if not isinstance(transport, asyncio.DatagramTransport):
                transport.close()
                raise RuntimeError("UDP 发现未返回 DatagramTransport")
            datagram_transport = transport
            self._transports.append(datagram_transport)
            datagram_transport.sendto(self.local.to_bytes(), ("255.255.255.255", self.local.port))

    async def stop(self) -> None:
        for transport in self._transports:
            transport.close()
        self._transports.clear()

    async def peers(self) -> AsyncIterator[tuple[DiscoveredPeer, ...]]:
        while self._transports:
            yield await self._queue.get()


@dataclass(frozen=True, slots=True)
class ProbeResult:
    source_ip: str
    target_ip: str
    adapter_name: str
    adapter_index: int
    latency_ms: float | None
    stage: str
    error: str | None


class ConnectionProbe:
    async def run(self, target: Endpoint, adapter: AdapterInfo) -> ProbeResult:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        started = time.perf_counter()
        try:
            bind_socket(sock, adapter)
            await asyncio.wait_for(
                asyncio.get_running_loop().sock_connect(sock, (target.host, target.port)), timeout=3
            )
            return ProbeResult(
                adapter.ipv4,
                target.host,
                adapter.name,
                adapter.index,
                (time.perf_counter() - started) * 1000,
                "connected",
                None,
            )
        except (OSError, TimeoutError) as exc:
            return ProbeResult(
                adapter.ipv4,
                target.host,
                adapter.name,
                adapter.index,
                None,
                "connect",
                str(exc),
            )
        finally:
            sock.close()
