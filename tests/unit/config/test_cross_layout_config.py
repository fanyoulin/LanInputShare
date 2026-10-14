from __future__ import annotations

import pytest

from lan_input_share.config.models import AppConfig, ConfigError, PeerTarget
from lan_input_share.core.models import DeviceRole, ScreenPosition


def controller_config(*peers: PeerTarget) -> AppConfig:
    return AppConfig(
        version=2,
        device_name="desk",
        role=DeviceRole.CONTROLLER,
        adapter_id=None,
        bind_ip=None,
        port=24870,
        local_position=ScreenPosition.CENTER,
        peers=peers,
    )


def test_controller_requires_a_local_position() -> None:
    with pytest.raises(ConfigError, match="主控必须设置本机位置"):
        AppConfig(
            version=2,
            device_name="desk",
            role=DeviceRole.CONTROLLER,
            adapter_id=None,
            bind_ip=None,
            port=24870,
            local_position=None,
            peers=(),
        )


def test_client_cannot_store_positions_or_controller_targets() -> None:
    peer = PeerTarget(ScreenPosition.RIGHT, "192.168.110.157")

    with pytest.raises(ConfigError, match="客户端不能配置屏幕位置或目标"):
        AppConfig(
            version=2,
            device_name="client",
            role=DeviceRole.CLIENT,
            adapter_id=None,
            bind_ip=None,
            port=24870,
            local_position=ScreenPosition.LEFT,
            peers=(peer,),
        )


@pytest.mark.parametrize("host", ["", "localhost", "300.1.1.1", "::1"])
def test_peer_target_requires_ipv4(host: str) -> None:
    with pytest.raises(ConfigError, match="客户端 IP"):
        PeerTarget(ScreenPosition.RIGHT, host)


@pytest.mark.parametrize("port", [0, 65536])
def test_peer_target_requires_valid_port(port: int) -> None:
    with pytest.raises(ConfigError, match="客户端端口"):
        PeerTarget(ScreenPosition.RIGHT, "192.168.110.157", port)


def test_controller_rejects_duplicate_positions_and_hosts() -> None:
    with pytest.raises(ConfigError, match="客户端位置不能重复"):
        controller_config(
            PeerTarget(ScreenPosition.RIGHT, "192.168.110.157"),
            PeerTarget(ScreenPosition.RIGHT, "192.168.110.158"),
        )

    with pytest.raises(ConfigError, match="客户端 IP 不能重复"):
        controller_config(
            PeerTarget(ScreenPosition.LEFT, "192.168.110.157"),
            PeerTarget(ScreenPosition.RIGHT, "192.168.110.157"),
        )


def test_controller_rejects_more_than_two_clients() -> None:
    with pytest.raises(ConfigError, match="最多配置两个客户端"):
        controller_config(
            PeerTarget(ScreenPosition.LEFT, "192.168.110.151"),
            PeerTarget(ScreenPosition.RIGHT, "192.168.110.152"),
            PeerTarget(ScreenPosition.TOP, "192.168.110.153"),
        )


def test_controller_rejects_peer_in_local_slot() -> None:
    with pytest.raises(ConfigError, match="客户端不能占用主控位置"):
        controller_config(PeerTarget(ScreenPosition.CENTER, "192.168.110.157"))


def test_peer_target_normalizes_optional_device_id() -> None:
    target = PeerTarget(
        ScreenPosition.RIGHT,
        "192.168.110.157",
        expected_device_id="  client-id  ",
    )

    assert target.expected_device_id == "client-id"
