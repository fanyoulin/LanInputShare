from __future__ import annotations

import json
import os
from pathlib import Path

from lan_input_share.config.models import AppConfig, ConfigError


class ConfigStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> AppConfig:
        if not self._path.exists():
            raise ConfigError(f"配置文件不存在：{self._path}")
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ConfigError("配置根节点必须是对象")
            return AppConfig.from_dict(payload)
        except ConfigError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"读取配置失败：{exc}") from exc

    def save(self, config: AppConfig) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.tmp")
        data = json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n"
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        except OSError as exc:
            raise ConfigError(f"保存配置失败：{exc}") from exc
        finally:
            temporary.unlink(missing_ok=True)
