from __future__ import annotations

import ipaddress
import socket
import struct
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum

import psutil


class AdapterKind(StrEnum):
    PHYSICAL = "physical"
    VIRTUAL = "virtual"
    LOOPBACK = "loopback"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class AdapterInfo:
    adapter_id: str
    name: str
    index: int
    ipv4: str
    is_up: bool
    metric: int


_VIRTUAL_TOKENS = (
    "clash",
    "wintun",
    "wireguard",
    "vpn",
    "hyper-v",
    "vethernet",
    "wsl",
    "tap-windows",
    "tailscale",
    "zerotier",
)
_RFC1918_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def classify_adapter(adapter: AdapterInfo) -> AdapterKind:
    if not adapter.is_up:
        return AdapterKind.UNAVAILABLE
    try:
        address = ipaddress.ip_address(adapter.ipv4)
    except ValueError:
        return AdapterKind.UNSUPPORTED
    if not isinstance(address, ipaddress.IPv4Address):
        return AdapterKind.UNSUPPORTED
    if address.is_loopback:
        return AdapterKind.LOOPBACK
    lowered = f"{adapter.adapter_id} {adapter.name}".lower()
    if any(token in lowered for token in _VIRTUAL_TOKENS):
        return AdapterKind.VIRTUAL
    if not any(address in network for network in _RFC1918_NETWORKS):
        return AdapterKind.UNSUPPORTED
    return AdapterKind.PHYSICAL


def choose_adapters(
    adapters: list[AdapterInfo] | tuple[AdapterInfo, ...], manual_id: str | None
) -> tuple[AdapterInfo, ...]:
    if manual_id is not None:
        return tuple(adapter for adapter in adapters if adapter.adapter_id == manual_id)
    physical = [
        adapter for adapter in adapters if classify_adapter(adapter) is AdapterKind.PHYSICAL
    ]
    return tuple(sorted(physical, key=lambda adapter: (adapter.metric, adapter.name.lower())))


def selected_ipv4(
    adapters: list[AdapterInfo] | tuple[AdapterInfo, ...], manual_id: str | None
) -> str | None:
    selected = choose_adapters(adapters, manual_id)
    return selected[0].ipv4 if selected else None


def list_adapters() -> tuple[AdapterInfo, ...]:
    addresses = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    result: list[AdapterInfo] = []
    for name, items in addresses.items():
        try:
            index = socket.if_nametoindex(name)
        except OSError:
            index = 0
        for item in items:
            if item.family == socket.AF_INET:
                result.append(
                    AdapterInfo(
                        adapter_id=name,
                        name=name,
                        index=index,
                        ipv4=item.address,
                        is_up=bool(stats.get(name) and stats[name].isup),
                        metric=index or 9999,
                    )
                )
    return tuple(result)


def bind_socket(sock: socket.socket, adapter: AdapterInfo, local_port: int = 0) -> None:
    sock.bind((adapter.ipv4, local_port))
    if adapter.index > 0 and hasattr(socket, "IP_UNICAST_IF"):
        with suppress(OSError):
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_UNICAST_IF,
                struct.pack("!I", adapter.index),
            )
