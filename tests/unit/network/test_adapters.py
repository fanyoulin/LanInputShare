import socket

import pytest

from lan_input_share.network.adapters import (
    AdapterInfo,
    AdapterKind,
    bind_socket,
    choose_adapters,
    classify_adapter,
)


def _adapter(name, address="192.168.1.10", *, metric=10, up=True, index=4):
    return AdapterInfo(name, name, index, address, up, metric)


@pytest.mark.parametrize(
    "name", ["Clash", "Wintun Userspace Tunnel", "Hyper-V", "vEthernet (WSL)"]
)
def test_virtual_adapter_is_excluded_by_default(name):
    assert classify_adapter(_adapter(name)) is AdapterKind.VIRTUAL
    assert choose_adapters([_adapter(name)], None) == ()


def test_ethernet_and_wifi_are_preferred_even_when_tun_has_lower_metric():
    tun = _adapter("Clash", "198.18.0.1", metric=1)
    wifi = _adapter("Wi-Fi", metric=20)
    ethernet = _adapter("Ethernet", "192.168.1.11", metric=10, index=5)

    assert choose_adapters([tun, wifi, ethernet], None) == (ethernet, wifi)


def test_manual_override_can_select_virtual_adapter():
    tun = _adapter("Clash", "198.18.0.1", metric=1)

    assert choose_adapters([tun], "Clash") == (tun,)


def test_down_loopback_and_public_address_are_not_automatic_candidates():
    adapters = [
        _adapter("Ethernet", up=False),
        _adapter("Loopback", "127.0.0.1"),
        _adapter("Ethernet 2", "8.8.8.8"),
    ]
    assert choose_adapters(adapters, None) == ()


def test_benchmark_range_used_by_tun_is_not_a_physical_lan_candidate():
    adapter = _adapter("Meta", "198.18.0.1")

    assert classify_adapter(adapter) is AdapterKind.UNSUPPORTED
    assert choose_adapters([adapter], None) == ()


def test_bind_socket_uses_adapter_ipv4():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        bind_socket(sock, _adapter("Loopback", "127.0.0.1", index=1))
        assert sock.getsockname()[0] == "127.0.0.1"
    finally:
        sock.close()
