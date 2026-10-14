from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_RULE_NAME = "LanInputShare Private LAN"


@dataclass(frozen=True, slots=True)
class FirewallStatus:
    configured: bool
    private_only: bool
    detail: str


type Runner = Callable[..., subprocess.CompletedProcess[str]]


class FirewallAdvisor:
    def __init__(self, *, runner: Runner = subprocess.run) -> None:
        self._runner = runner

    def inspect(self, port: int) -> FirewallStatus:
        _validate_port(port)
        result = self._runner(
            [_netsh(), "advfirewall", "firewall", "show", "rule", f"name={_RULE_NAME}"],
            capture_output=True,
            text=True,
            check=False,
        )
        output = f"{result.stdout}\n{result.stderr}"
        lowered = output.casefold()
        configured = result.returncode == 0 and f"{port}" in lowered
        private_only = configured and "private" in lowered and "public" not in lowered
        return FirewallStatus(configured, private_only, output.strip())

    def private_network_command(self, executable: Path, port: int) -> list[str]:
        _validate_port(port)
        return [
            _netsh(),
            "advfirewall",
            "firewall",
            "add",
            "rule",
            f"name={_RULE_NAME}",
            "dir=in",
            "action=allow",
            f"program={executable.resolve()}",
            "protocol=TCP",
            f"localport={port}",
            "profile=private",
            "enable=yes",
        ]

    def apply_private_rule(self, executable: Path, port: int) -> FirewallStatus:
        command = self.private_network_command(executable, port)
        result = self._runner(command, capture_output=True, text=True, check=False)
        output = f"{result.stdout}\n{result.stderr}".strip()
        return FirewallStatus(result.returncode == 0, result.returncode == 0, output)


def _netsh() -> str:
    system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    return str(system_root / "System32" / "netsh.exe")


def _validate_port(port: int) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1 到 65535 之间")


__all__ = ["FirewallAdvisor", "FirewallStatus"]
