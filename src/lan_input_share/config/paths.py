from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path
    config_file: Path
    log_dir: Path
    identity_dir: Path
    staging_dir: Path

    @classmethod
    def from_environment(cls) -> AppPaths:
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise RuntimeError("LOCALAPPDATA 未设置")
        root = Path(local_app_data) / "LanInputShare"
        return cls(
            root=root,
            config_file=root / "config.json",
            log_dir=root / "logs",
            identity_dir=root / "identity",
            staging_dir=root / "staging",
        )
