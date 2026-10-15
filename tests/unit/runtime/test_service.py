from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

import lan_input_share.runtime.service as service_module
from lan_input_share.config.models import AppConfig
from lan_input_share.config.paths import AppPaths
from lan_input_share.core.models import DeviceRole
from lan_input_share.elevated.models import HelperState, HelperStatus
from lan_input_share.runtime.service import NetworkRoleService


class FakeHelper:
    def __init__(self) -> None:
        self.status = HelperStatus(HelperState.RUNNING, "ready")
        self.callbacks: list[Callable[[HelperStatus], None]] = []
        self.starts = 0
        self.stops = 0

    def add_status_callback(self, callback: Callable[[HelperStatus], None]) -> None:
        self.callbacks.append(callback)

    def inject(self, event: object) -> None:
        return

    def release_all(self, events: tuple[object, ...]) -> None:
        return

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1


class FakeLocal:
    def inject(self, event: object) -> None:
        return

    def release_all(self, events: tuple[object, ...]) -> None:
        return


@pytest.mark.asyncio
async def test_client_helper_lifecycle_has_no_duplicate_threads(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    helper = FakeHelper()
    statuses: list[HelperStatus] = []
    monkeypatch.setattr(service_module, "Win32InputInjector", FakeLocal)
    service = NetworkRoleService(
        AppConfig.default(device_name="client", role=DeviceRole.CLIENT),
        _paths(tmp_path),
        helper_factory=lambda: helper,  # type: ignore[arg-type]
        helper_status=statuses.append,
    )

    service._ensure_client_input()
    service._ensure_client_input()

    assert helper.starts == 1
    assert statuses[-1].state is HelperState.RUNNING
    await service.stop()
    assert helper.stops == 1


def _paths(root: Path) -> AppPaths:
    return AppPaths(
        root=root,
        config_file=root / "config.json",
        log_dir=root / "logs",
        identity_dir=root / "identity",
        staging_dir=root / "staging",
    )
