import dataclasses
import json

import pytest

from lan_input_share.config.models import AppConfig, ConfigError
from lan_input_share.config.store import ConfigStore
from lan_input_share.core.models import DeviceRole, ScreenPosition
from lan_input_share.ui.close_behavior import CloseChoice, ClosePreference


def test_config_round_trip_uses_version_two(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    config = AppConfig.default(device_name="desk-main")

    store.save(config)

    assert store.load() == config
    payload = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert payload["role"] == "client"
    assert payload["port"] == 24870
    assert payload["local_position"] is None
    assert payload["peers"] == []
    assert "controller_host" not in payload
    assert "layout" not in payload
    assert payload["staging_retention_hours"] == 24


def test_close_preference_round_trips_in_version_two_config(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    config = dataclasses.replace(
        AppConfig.default(device_name="Desk"),
        close_preference=ClosePreference(CloseChoice.MINIMIZE_TO_TRAY),
    )

    store.save(config)

    assert store.load().close_preference.remembered is CloseChoice.MINIMIZE_TO_TRAY


def test_save_replaces_file_atomically_without_leaving_temp_file(tmp_path):
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    store.save(AppConfig.default(device_name="first"))
    replacement = AppConfig.default(device_name="second", role=DeviceRole.CONTROLLER)

    store.save(replacement)

    assert store.load() == replacement
    assert list(tmp_path.iterdir()) == [path]


def test_version_one_controller_is_migrated_in_memory(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "device_name": "old-controller",
                "role": "controller",
                "adapter_id": "WLAN",
                "bind_ip": "192.168.110.156",
                "controller_host": None,
                "port": 24900,
                "layout": ["old-controller"],
                "hotkeys": {"emergency_return": "Ctrl+Alt+Esc"},
                "staging_retention_hours": 48,
            }
        ),
        encoding="utf-8",
    )

    config = ConfigStore(path).load()

    assert config.version == 2
    assert config.device_name == "old-controller"
    assert config.role is DeviceRole.CONTROLLER
    assert config.adapter_id == "WLAN"
    assert config.bind_ip == "192.168.110.156"
    assert config.port == 24900
    assert config.local_position is ScreenPosition.CENTER
    assert config.peers == ()
    assert config.staging_retention_hours == 48


def test_version_one_client_drops_controller_host_when_migrated(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "device_name": "old-client",
                "role": "client",
                "adapter_id": None,
                "bind_ip": None,
                "controller_host": "192.168.110.156",
                "port": 24870,
                "layout": [],
            }
        ),
        encoding="utf-8",
    )

    config = ConfigStore(path).load()

    assert config.version == 2
    assert config.role is DeviceRole.CLIENT
    assert config.local_position is None
    assert config.peers == ()


def test_corrupt_config_raises_config_error_instead_of_resetting_silently(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(ConfigError, match="读取配置失败"):
        ConfigStore(path).load()


def test_missing_config_raises_actionable_error(tmp_path):
    with pytest.raises(ConfigError, match="配置文件不存在"):
        ConfigStore(tmp_path / "missing.json").load()
