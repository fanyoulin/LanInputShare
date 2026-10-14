from __future__ import annotations

from lan_input_share.windows.startup import StartupManager


class FakeRegistry:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set_current_user(self, key: str, name: str, value: str) -> None:
        self.values[(key, name)] = value

    def get_current_user(self, key: str, name: str) -> str | None:
        return self.values.get((key, name))

    def delete_current_user(self, key: str, name: str) -> None:
        self.values.pop((key, name), None)


def test_startup_uses_current_user_run_key_and_exact_quoted_command() -> None:
    registry = FakeRegistry()
    manager = StartupManager(registry=registry)

    manager.enable((r"C:\Program Files\LAN Share\lan-input-share.exe", "--minimized"))

    assert registry.values == {
        (
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            "LanInputShare",
        ): r'"C:\Program Files\LAN Share\lan-input-share.exe" --minimized'
    }
    assert manager.status().enabled is True
    manager.disable()
    assert manager.status().enabled is False
