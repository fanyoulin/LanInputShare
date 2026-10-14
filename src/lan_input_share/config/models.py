from __future__ import annotations

import ipaddress
from dataclasses import asdict, dataclass, field
from typing import Any

from lan_input_share.core.models import DeviceRole, HotkeyConfig, ScreenPosition
from lan_input_share.ui.close_behavior import CloseChoice, ClosePreference


class ConfigError(RuntimeError):
    """Raised when configuration cannot be loaded or validated."""


@dataclass(frozen=True, slots=True)
class PeerTarget:
    position: ScreenPosition
    host: str
    port: int = 24870
    expected_device_id: str | None = None

    def __post_init__(self) -> None:
        host = self.host.strip()
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ConfigError("客户端 IP 必须是有效的 IPv4 地址") from exc
        if address.version != 4:
            raise ConfigError("客户端 IP 必须是有效的 IPv4 地址")
        if not 1 <= self.port <= 65535:
            raise ConfigError("客户端端口必须在 1 到 65535 之间")
        object.__setattr__(self, "host", str(address))
        object.__setattr__(self, "expected_device_id", _optional_text(self.expected_device_id))


@dataclass(frozen=True, slots=True)
class AppConfig:
    version: int
    device_name: str
    role: DeviceRole
    adapter_id: str | None
    bind_ip: str | None
    port: int
    local_position: ScreenPosition | None = None
    peers: tuple[PeerTarget, ...] = ()
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)
    staging_retention_hours: int = 24
    close_preference: ClosePreference = field(default_factory=ClosePreference)
    # Transitional constructor/read compatibility for the preview runtime.
    # Version-2 persistence never writes these fields.
    controller_host: str | None = field(default=None, repr=False, compare=False)
    layout: tuple[str, ...] = field(default=(), repr=False, compare=False)

    @classmethod
    def default(
        cls,
        *,
        device_name: str,
        role: DeviceRole = DeviceRole.CLIENT,
    ) -> AppConfig:
        return cls(
            version=2,
            device_name=device_name,
            role=role,
            adapter_id=None,
            bind_ip=None,
            port=24870,
            local_position=(
                ScreenPosition.CENTER if role is DeviceRole.CONTROLLER else None
            ),
        )

    def __post_init__(self) -> None:
        if self.version not in (1, 2):
            raise ConfigError(f"不支持的配置版本：{self.version}")
        if not self.device_name.strip():
            raise ConfigError("设备名称不能为空")
        if not 1 <= self.port <= 65535:
            raise ConfigError("端口必须在 1 到 65535 之间")
        if self.staging_retention_hours <= 0:
            raise ConfigError("暂存保留时间必须为正数")
        if self.version == 1:
            if len(self.layout) > 3 or len(set(self.layout)) != len(self.layout):
                raise ConfigError("设备排列最多三台且不能重复")
            return
        if self.role is DeviceRole.CONTROLLER and self.local_position is None:
            raise ConfigError("主控必须设置本机位置")
        if self.role is DeviceRole.CLIENT and (
            self.local_position is not None or self.peers
        ):
            raise ConfigError("客户端不能配置屏幕位置或目标")
        if len(self.peers) > 2:
            raise ConfigError("最多配置两个客户端")
        positions = tuple(peer.position for peer in self.peers)
        if len(set(positions)) != len(positions):
            raise ConfigError("客户端位置不能重复")
        hosts = tuple(peer.host for peer in self.peers)
        if len(set(hosts)) != len(hosts):
            raise ConfigError("客户端 IP 不能重复")
        if self.local_position is not None and self.local_position in positions:
            raise ConfigError("客户端不能占用主控位置")

    def to_dict(self) -> dict[str, Any]:
        local_position = self.local_position
        peers = self.peers
        if self.version == 1:
            local_position = (
                ScreenPosition.CENTER
                if self.role is DeviceRole.CONTROLLER
                else None
            )
            peers = ()
        return {
            "version": 2,
            "device_name": self.device_name,
            "role": self.role.value,
            "adapter_id": self.adapter_id,
            "bind_ip": self.bind_ip,
            "port": self.port,
            "local_position": (
                None if local_position is None else local_position.value
            ),
            "peers": [
                {
                    "position": peer.position.value,
                    "host": peer.host,
                    "port": peer.port,
                    "expected_device_id": peer.expected_device_id,
                }
                for peer in peers
            ],
            "hotkeys": asdict(self.hotkeys),
            "staging_retention_hours": self.staging_retention_hours,
            "close_preference": (
                None
                if self.close_preference.remembered is None
                else self.close_preference.remembered.value
            ),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AppConfig:
        try:
            version = int(payload["version"])
            hotkeys_payload = payload.get("hotkeys", {})
            if not isinstance(hotkeys_payload, dict):
                raise TypeError("hotkeys 必须是对象")
            role = DeviceRole(str(payload["role"]))
            device_name = str(payload["device_name"])
            adapter_id = _optional_text(payload.get("adapter_id"))
            bind_ip = _optional_text(payload.get("bind_ip"))
            port = int(payload["port"])
            hotkeys = HotkeyConfig(**hotkeys_payload)
            retention = int(payload.get("staging_retention_hours", 24))
            raw_close_preference = payload.get("close_preference")
            close_preference = ClosePreference(
                None
                if raw_close_preference is None
                else CloseChoice(str(raw_close_preference))
            )
            if version == 1:
                return cls(
                    version=2,
                    device_name=device_name,
                    role=role,
                    adapter_id=adapter_id,
                    bind_ip=bind_ip,
                    port=port,
                    local_position=(
                        ScreenPosition.CENTER
                        if role is DeviceRole.CONTROLLER
                        else None
                    ),
                    peers=(),
                    hotkeys=hotkeys,
                    staging_retention_hours=retention,
                    close_preference=close_preference,
                )
            if version != 2:
                raise ConfigError(f"不支持的配置版本：{version}")
            raw_peers = payload.get("peers", [])
            if not isinstance(raw_peers, list):
                raise TypeError("peers 必须是数组")
            peers = tuple(
                PeerTarget(
                    position=ScreenPosition(str(item["position"])),
                    host=str(item["host"]),
                    port=int(item.get("port", 24870)),
                    expected_device_id=_optional_text(item.get("expected_device_id")),
                )
                for item in raw_peers
                if isinstance(item, dict)
            )
            raw_position = payload.get("local_position")
            return cls(
                version=2,
                device_name=device_name,
                role=role,
                adapter_id=adapter_id,
                bind_ip=bind_ip,
                port=port,
                local_position=(
                    None if raw_position is None else ScreenPosition(str(raw_position))
                ),
                peers=peers,
                hotkeys=hotkeys,
                staging_retention_hours=retention,
                close_preference=close_preference,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"配置内容无效：{exc}") from exc


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
